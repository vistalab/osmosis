"""

This is a script for segmenting the corpus callosum from all the brains
from the human connectome project.

"""
from dipy.segment.mask import bounding_box
import nibabel as nib
import numpy as np
import os
from dipy.segment.mask import median_otsu
from dipy.core.gradients import gradient_table
import osmosis.model.dti as dti
import subprocess as sp
import osmosis.utils as ozu
from dipy.reconst.dti import TensorModel

sid_list = ["103414", "105115", "110411", "111312", "113619",
            "115320", "117122", "118730", "118932"]

for sid in sid_list:
    # Load the data for this particular subject.
    data_path = "/hsgs/projects/wandell/klchan13/hcp_data_q3/%s/T1w/Diffusion/"%sid
    data_file = nib.load(os.path.join(data_path, "data.nii.gz"))
    data = data_file.get_data()

    # Load b values and b vectors
    bvals = np.loadtxt(os.path.join(data_path, "bvals"))
    bvecs = np.loadtxt(os.path.join(data_path, "bvecs"))

    # Separate the b-values and find the indices.
    bval_list, b_inds, unique_b, bvals_scaled = ozu.separate_bvals(bvals)
    all_b_idx = np.where(bvals_scaled != 0)

    ad_arr = np.zeros(3)
    rd_arr = np.zeros(3)
    for b_idx in np.arange(1, len(unique_b)):
        # Separate data by b-value and create a b0 mask.
        bnk_b0_inds = np.concatenate((b_inds[0], b_inds[b_idx]))
        bnk_data = data[..., bnk_b0_inds]
        b0_mask, mask = median_otsu(data[..., b_inds[0][0]], 4, 4)

        # Fit a tensor for generating a color FA map
        gtab = gradient_table(bvals[bnk_b0_inds], bvecs[:, bnk_b0_inds])
        tenmodel = TensorModel(gtab)
        tensorfit = tenmodel.fit(bnk_data, mask=mask)

        # Now segment the corpus callosum
        threshold = (0.5, 1, 0, 0.2, 0, 0.2)
        CC_box = np.zeros_like(data[..., b_inds[0][0]])

        # Create a bounding box in which to look for the corpus
        # callosum.
        mins, maxs = bounding_box(mask)
        mins = np.array(mins)
        maxs = np.array(maxs)
        diff = (maxs - mins) // 5
        bounds_min = mins + diff
        bounds_max = maxs - diff

        CC_box[bounds_min[0]:bounds_max[0],
               bounds_min[1]:bounds_max[1],
               bounds_min[2]:bounds_max[2]] = 1

        mask_corpus_callosum, cfa = segment_from_cfa(tensorfit, CC_box,
                                                     threshold,
                                                     return_cfa=True)

        # Clean up the cc isolation
        new_mask = isolate_cc(mask_corpus_callosum)

        tm = dti.TensorModel(bnk_data, bvecs[:, bnk_b0_inds],
                             bvals[bnk_b0_inds], mask=new_mask,
                             params_file = "temp")

        # Extract the median axial and radial diffusivities for a tensor fit to
        # the voxels in the corpus callosum.
        ad_arr[b_idx-1] = np.median(tm.axial_diffusivity[np.where(new_mask)])
        rd_arr[b_idx-1] = np.median(tm.radial_diffusivity[np.where(new_mask)])

    os.chdir(data_path)

    # Save a mask of the corpus callsoum.
    aff = data_file.get_affine()
    nib.Nifti1Image(new_mask).to_filename(os.path.join(data_path,
                                                "cc_mask.nii.gz"))

    # Store axial and radial diffusivities in a text file.
    nib.data_file.get_affine()
    ad_rd = open("ad_rd_%s.txt"%sid, "w")
    ad_rd = ad_rd.write("%s\n%s"%(ad_arr, rd_arr))
