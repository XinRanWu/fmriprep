#!/usr/bin/env python
# -*- coding: utf-8 -*-
# emacs: -*- mode: python; py-indent-offset: 4; indent-tabs-mode: nil -*-
# vi: set ft=python sts=4 ts=4 sw=4 et:
"""
Apply susceptibility distortion correction (SDC)
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^


.. topic :: Abbreviations

    fmap
        fieldmap
    VSM
        voxel-shift map -- a 3D nifti where displacements are in pixels (not mm)
    DFM
        displacements field map -- a nifti warp file compatible with ANTs (mm)

"""
from __future__ import print_function, division, absolute_import, unicode_literals

import pkg_resources as pkgr

from nipype.pipeline import engine as pe
from nipype.interfaces import utility as niu
from nipype.interfaces import fsl
from nipype.interfaces.ants import CreateJacobianDeterminantImage
from niworkflows.interfaces.registration import ANTSApplyTransformsRPT, ANTSRegistrationRPT
from niworkflows.interfaces.masks import ComputeEPIMask

from fmriprep.interfaces import itk
from fmriprep.interfaces import ReadSidecarJSON
from fmriprep.interfaces.bids import DerivativesDataSink

from nipype.interfaces import ants
from fmriprep.interfaces import CopyHeader

def init_sdc_unwarp_wf(reportlets_dir, ants_nthreads, fmap_bspline,
                       fmap_demean, debug, name='sdc_unwarp_wf'):
    """
    This workflow takes in a displacements fieldmap and calculates the corresponding
    displacements field (in other words, an ANTs-compatible warp file).
    
    It also calculates a new mask for the input dataset that takes into account the distortions.
    The mask is restricted to the field of view of the fieldmap since outside of it corrections could not be performed.

    .. workflow ::

        from fmriprep.workflows.fieldmap.unwarp import init_sdc_unwarp_wf
        wf = init_sdc_unwarp_wf(reportlets_dir='.', ants_nthreads=8,
                                fmap_bspline=False, fmap_demean=True,
                                debug=False)


    Inputs

        in_reference
            the reference image
        in_mask
            a brain mask corresponding to ``in_reference``
        name_source
            path to the original _bold file being unwarped
        fmap
            the fieldmap in Hz
        fmap_ref
            the reference (anatomical) image corresponding to ``fmap``
        fmap_mask
            a brain mask corresponding to ``fmap``


    Outputs

        out_reference
            the ``in_reference`` after unwarping
        out_warp
            the corresponding :abbr:`DFM (displacements field map)` compatible with
            ANTs
        out_jacobian
            the jacobian of the field (for drop-out alleviation)
        out_mask
            mask of the unwarped input file
        out_mask_report
            reportled for the skullstripping

    """

    workflow = pe.Workflow(name=name)
    inputnode = pe.Node(niu.IdentityInterface(
        fields=['in_reference', 'in_mask', 'name_source',
                'fmap_ref', 'fmap_mask', 'fmap']), name='inputnode')
    outputnode = pe.Node(niu.IdentityInterface(
        fields=['out_reference', 'out_warp', 'out_mask',
                'out_jacobian', 'out_mask_report']), name='outputnode')

    meta = pe.Node(ReadSidecarJSON(), name='meta')

    explicit_mask_epi = pe.Node(fsl.ApplyMask(), name="explicit_mask_epi")

    # Register the reference of the fieldmap to the reference
    # of the target image (the one that shall be corrected)
    ants_settings = pkgr.resource_filename('fmriprep', 'data/fmap-any_registration.json')
    if debug:
        ants_settings = pkgr.resource_filename(
            'fmriprep', 'data/fmap-any_registration_testing.json')
    fmap2ref_reg = pe.Node(ANTSRegistrationRPT(generate_report=True,
        from_file=ants_settings, output_inverse_warped_image=True,
        output_warped_image=True, num_threads=ants_nthreads),
                       name='fmap2ref_reg')
    fmap2ref_reg.interface.num_threads = ants_nthreads

    ds_reg = pe.Node(
        DerivativesDataSink(base_directory=reportlets_dir,
                            suffix='fmap_reg'), name='ds_reg')

    # Map the VSM into the EPI space
    fmap2ref_apply = pe.Node(ANTSApplyTransformsRPT(
        generate_report=True, dimension=3, interpolation='BSpline', float=True),
                             name='fmap2ref_apply')

    fmap_mask2ref_apply = pe.Node(ANTSApplyTransformsRPT(
        generate_report=False, dimension=3, interpolation='NearestNeighbor',
        float=True),
        name='fmap_mask2ref_apply')

    ds_reg_vsm = pe.Node(
        DerivativesDataSink(base_directory=reportlets_dir,
                            suffix='fmap_reg_vsm'), name='ds_reg_vsm')

    # Fieldmap to rads and then to voxels (VSM - voxel shift map)
    torads = pe.Node(niu.Function(function=_hz2rads), name='torads')

    gen_vsm = pe.Node(fsl.FUGUE(save_unmasked_shift=True), name='gen_vsm')
    # Convert the VSM into a DFM (displacements field map)
    # or: FUGUE shift to ANTS warping.
    vsm2dfm = pe.Node(itk.FUGUEvsm2ANTSwarp(), name='vsm2dfm')
    jac_dfm = pe.Node(CreateJacobianDeterminantImage(
        imageDimension=3, outputImage='jacobian.nii.gz'), name='jac_dfm')

    unwarp_reference = pe.Node(ANTSApplyTransformsRPT(dimension=3,
                                                      generate_report=False,
                                                      float=True,
                                                      interpolation='LanczosWindowedSinc'),
                               name='unwarp_reference')

    fieldmap_fov_mask = pe.Node(niu.Function(function=_fill_with_ones), name='fieldmap_fov_mask')

    fmap_fov2ref_apply = pe.Node(ANTSApplyTransformsRPT(
        generate_report=False, dimension=3, interpolation='NearestNeighbor',
        float=True),
        name='fmap_fov2ref_apply')

    apply_fov_mask = pe.Node(fsl.ApplyMask(), name="apply_fov_mask")

    ref_msk_post = pe.Node(ComputeEPIMask(generate_report=True, dilation=1),
                           name='ref_msk_post')

    workflow.connect([
        (inputnode, meta, [('name_source', 'in_file')]),
        (inputnode, explicit_mask_epi, [('in_reference', 'in_file'),
                                        ('in_mask', 'mask_file')]),
        (inputnode, fmap2ref_reg, [('fmap_ref', 'moving_image')]),
        (inputnode, fmap2ref_apply, [('in_reference', 'reference_image')]),
        (fmap2ref_reg, fmap2ref_apply, [
            ('composite_transform', 'transforms')]),
        (inputnode, fmap_mask2ref_apply, [('in_reference', 'reference_image')]),
        (fmap2ref_reg, fmap_mask2ref_apply, [
            ('composite_transform', 'transforms')]),
        (inputnode, ds_reg_vsm, [('name_source', 'source_file')]),
        (fmap2ref_apply, ds_reg_vsm, [('out_report', 'in_file')]),
        (explicit_mask_epi, fmap2ref_reg, [('out_file', 'fixed_image')]),
        (inputnode, ds_reg, [('name_source', 'source_file')]),
        (fmap2ref_reg, ds_reg, [('out_report', 'in_file')]),
        (inputnode, fmap2ref_apply, [('fmap', 'input_image')]),
        (inputnode, fmap_mask2ref_apply, [('fmap_mask', 'input_image')]),
        (fmap2ref_apply, torads, [('output_image', 'in_file')]),
        (meta, gen_vsm, [(('out_dict', _get_ec), 'dwell_time'),
                         (('out_dict', _get_pedir_fugue), 'unwarp_direction')]),
        (meta, vsm2dfm, [(('out_dict', _get_pedir_bids), 'pe_dir')]),
        (torads, gen_vsm, [('out', 'fmap_in_file')]),
        (vsm2dfm, unwarp_reference, [('out_file', 'transforms')]),
        (inputnode, unwarp_reference, [('in_reference', 'reference_image')]),
        (inputnode, unwarp_reference, [('in_reference', 'input_image')]),
        (vsm2dfm, outputnode, [('out_file', 'out_warp')]),
        (vsm2dfm, jac_dfm, [('out_file', 'deformationField')]),
        (inputnode, fieldmap_fov_mask, [('fmap_ref', 'in_file')]),
        (fieldmap_fov_mask, fmap_fov2ref_apply, [('out', 'input_image')]),
        (inputnode, fmap_fov2ref_apply, [('in_reference', 'reference_image')]),
        (fmap2ref_reg, fmap_fov2ref_apply, [('composite_transform', 'transforms')]),
        (fmap_fov2ref_apply, apply_fov_mask, [('output_image', 'mask_file')]),
        (unwarp_reference, apply_fov_mask, [('output_image', 'in_file')]),
        (apply_fov_mask, ref_msk_post, [('out_file', 'in_file')]),
        (apply_fov_mask, outputnode, [('out_file', 'out_reference')]),
        (ref_msk_post, outputnode, [('mask_file', 'out_mask')]),
        (ref_msk_post, outputnode, [('out_report', 'out_mask_report')]),
        (jac_dfm, outputnode, [('jacobian_image', 'out_jacobian')]),
    ])

    if not fmap_bspline:
        workflow.connect([
            (fmap_mask2ref_apply, gen_vsm, [('output_image', 'mask_file')])
        ])

    if fmap_demean:
        # Demean within mask
        demean = pe.Node(niu.Function(function=_demean), name='demean')

        workflow.connect([
            (gen_vsm, demean, [('shift_out_file', 'in_file')]),
            (fmap_mask2ref_apply, demean, [('output_image', 'in_mask')]),
            (demean, vsm2dfm, [('out', 'in_file')]),
        ])

    else:
        workflow.connect([
            (gen_vsm, vsm2dfm, [('shift_out_file', 'in_file')]),
        ])

    return workflow


def init_pepolar_unwarp_report_wf(fmaps, bids_dir, name="pepolar_unwarp_wf"):

    workflow = pe.Workflow(name=name)
    inputnode = pe.Node(niu.IdentityInterface(
        fields=['in_reference', 'in_mask', 'name_source']), name='inputnode')

    outputnode = pe.Node(niu.IdentityInterface(
        fields=['out_reference', 'out_warp', 'out_mask',
                'out_jacobian', 'out_mask_report']), name='outputnode')

    mag_inu = pe.MapNode(ants.N4BiasFieldCorrection(dimension=3),
                         iterfield='input_image',
                         name='mag_inu')
    mag_inu.inputs.input_image = [fmap['epi'] for fmap in fmaps]
    cphdr = pe.MapNode(CopyHeader(), iterfield=['hdr_file', 'in_file'],
                       name='cphdr')
    cphdr.inputs.hdr_file = [fmap['epi'] for fmap in fmaps]

    skullstrip = pe.MapNode(ComputeEPIMask(dilation=1), iterfield='in_file',
                            name='skullstrip')

    apply_skullstrip = pe.MapNode(fsl.ApplyMask(),
                                  iterfield=['mask_file', 'in_file'],
                                  name="apply_skullstrip")
    apply_skullstrip.inputs.in_file = [fmap['epi'] for fmap in fmaps]

    explicit_mask_epi = pe.Node(fsl.ApplyMask(), name="explicit_mask_epi")

    merge_list = pe.Node(niu.Merge(2), name='merge_list')

    merge = pe.Node(fsl.Merge(dimension='t'), name="merge")

    generate_idata = pe.Node(niu.Function(function=_generate_idata),
                             name='generate_idata')
    generate_idata.inputs.bids_dir = bids_dir
    generate_idata.inputs.fmaps = fmaps

    topup = pe.Node(fsl.TOPUP(), name='topup')

    to_ants = pe.Node(niu.Function(function=_add_dimension), name='to_ants')

    cphdr_warp = pe.Node(CopyHeader(), name='cphdr_warp')

    unwarp_reference = pe.Node(ANTSApplyTransformsRPT(dimension=3,
                                                      generate_report=False,
                                                      float=True,
                                                      interpolation='LanczosWindowedSinc'),
                               name='unwarp_reference')

    apply_jacobian = pe.Node(fsl.BinaryMaths(operation="mul"),
                             name='apply_jacobian')

    ref_msk_post = pe.Node(ComputeEPIMask(generate_report=True, dilation=1),
                           name='ref_msk_post')

    workflow.connect([
        (inputnode, explicit_mask_epi, [('in_reference', 'in_file'),
                                        ('in_mask', 'mask_file')]),
        #(explicit_mask_epi, merge_list, [('out_file', 'in1')]),
        (inputnode, merge_list, [('in_reference', 'in1')]),
        (mag_inu, cphdr, [('output_image', 'in_file')]),
        (cphdr, skullstrip, [('out_file', 'in_file')]),
        (skullstrip, apply_skullstrip, [('mask_file', 'mask_file')]),
        #(apply_skullstrip, merge_list, [('out_file', 'in2')]),
        (cphdr, merge_list, [('out_file', 'in2')]),
        (merge_list, merge, [('out', 'in_files')]),
        (inputnode, generate_idata, [('name_source', 'name_source')]),
        (generate_idata, topup, [('out', 'encoding_file')]),
        (merge, topup, [('merged_file', 'in_file')]),
        (inputnode, cphdr_warp, [('in_reference', 'hdr_file')]),
        (topup, cphdr_warp, [(('out_warps', _pick_first), 'in_file')]),
        (cphdr_warp, to_ants, [('out_file', 'in_file')]),
        (to_ants, unwarp_reference, [('out', 'transforms')]),
        (inputnode, unwarp_reference, [('in_reference', 'reference_image')]),
        (inputnode, unwarp_reference, [('in_reference', 'input_image')]),
        (unwarp_reference, apply_jacobian, [('output_image', 'in_file')]),
        (topup, apply_jacobian, [(('out_jacs', _pick_first), 'operand_file')]),
        (apply_jacobian, ref_msk_post, [('out_file', 'in_file')]),
        (unwarp_reference, outputnode, [('output_image', 'out_reference')]),
        (ref_msk_post, outputnode, [('mask_file', 'out_mask')]),
        (ref_msk_post, outputnode, [('out_report', 'out_mask_report')]),
        (to_ants, outputnode, [('out', 'out_warp')]),
        (topup, outputnode, [(('out_jacs', _pick_first), 'out_jacobian')]),
        ])

    return workflow


# Helper functions
# ------------------------------------------------------------

def _pick_first(l):
    return l[0]

def _add_dimension(in_file):
    import nibabel as nb
    import numpy as np
    import os

    nii = nb.load(in_file)
    hdr = nii.header.copy()
    hdr.set_data_dtype(np.dtype('<f4'))
    hdr.set_intent('vector', (), '')

    field = nii.get_data()
    field = field[:, :, :, np.newaxis, :]

    out_file = os.path.abspath("warpfield.nii.gz")

    nb.Nifti1Image(field.astype(np.dtype('<f4')), nii.affine, hdr).to_filename(out_file)

    return out_file


def _generate_idata(name_source, fmaps, bids_dir):
    import os
    from bids.grabbids import BIDSLayout
    out_file = os.path.abspath("idata.txt")

    layout = BIDSLayout(bids_dir)

    def _gen_line(in_file, oneline=False):
        import nibabel as nb
        lines = ''
        shape = nb.load(in_file).shape

        if len(shape) == 3 or oneline:
            vols = 1
        else:
            vols = nb.load(in_file).shape[3]
        vols = 1
        for i in range(vols):
            line_arr = ['0', '0', '0', '0']
            meta = layout.get_metadata(in_file)
            line_arr[3] = str(meta["TotalReadoutTime"])
            if meta["PhaseEncodingDirection"].endswith('-'):
                dir = '-1'
            else:
                dir = '1'
            line_arr[{'i':0, 'j':1, 'k':2}[meta["PhaseEncodingDirection"][0:1]]] = dir

            lines += ' '.join(line_arr) + '\n'
        return lines

    with open(out_file, 'w') as fp:
        fp.write(_gen_line(name_source, True))
        for fmap in fmaps:
            fp.write(_gen_line(fmap['epi']))

    return out_file


def _get_ec(in_dict):
    return float(in_dict['EffectiveEchoSpacing'])


def _get_pedir_bids(in_dict):
    return in_dict['PhaseEncodingDirection']


def _get_pedir_fugue(in_dict):
    return in_dict['PhaseEncodingDirection'].replace('i', 'x').replace('j', 'y').replace('k', 'z')


def _hz2rads(in_file, out_file=None):
    """Transform a fieldmap in Hz into rad/s"""
    from math import pi
    import nibabel as nb
    from fmriprep.utils.misc import genfname
    if out_file is None:
        out_file = genfname(in_file, 'rads')
    nii = nb.load(in_file)
    data = nii.get_data() * 2.0 * pi
    nb.Nifti1Image(data, nii.get_affine(),
                   nii.get_header()).to_filename(out_file)
    return out_file


def _demean(in_file, in_mask, out_file=None):
    import numpy as np
    import nibabel as nb
    from fmriprep.utils.misc import genfname

    if out_file is None:
        out_file = genfname(in_file, 'demeaned')
    nii = nb.load(in_file)
    msk = nb.load(in_mask).get_data()
    data = nii.get_data()
    data -= np.median(data[msk > 0])
    nb.Nifti1Image(data, nii.affine, nii.header).to_filename(
        out_file)
    return out_file


def _fill_with_ones(in_file):
    import nibabel as nb
    import numpy as np
    import os

    nii = nb.load(in_file)
    data = np.ones(nii.shape)

    out_name = os.path.abspath("out.nii.gz")
    nb.Nifti1Image(data, nii.affine, nii.header).to_filename(out_name)

    return out_name
