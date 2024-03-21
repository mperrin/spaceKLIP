import os
import numpy as np, matplotlib.pyplot as plt
import jwst
import webbpsf
import spaceKLIP.plotting, spaceKLIP.constants
import pysiaf

# =============================================================================
# Files for creating simulated PSFs mock data for synthetic RDI



def create_miri_synthetic_psf(science_filename, small_grid=None, verbose=True,
                              choice='closest',
                              nlambda=None, plot=True):
    """Generate a simulated PSF based on wavefront sensing data, suitable for synthetic RDI

    This will create and save one or more simulated files. The output files are
    saved in JWST-compatible format, so should be readable in a standard way
    interchangable with observed data.  Output filenames use the program and
    observation numbers derived from the WFS measurements, so will be distinct
    from the science data.


    Parameters
    ----------
    science_filename : str
        Filename of a science dataset to match. This is used to determine coronagraph mask, filter, etc
    small_grid : None or int
        Simulate small grid dithers? Set this to 5 or 9 to simulated the MIRI SGD with that number of data points
    verbose : bool
        Be more verbose in text output?
    choice : str
        Choice of which WFS sensing data to use. See webbpsf's load_wss_opd_by_date
    nlambda : int
        Number of wavelengths to simulate. Leave None to use the webbpsf default
    plot : bool
        Output a plot of the simulated synthetic PSF.

    Returns
    -------
    List of filenames of the output files


    """


    if verbose:
        print(f"*** Setting up MIRI simulation instance to match {os.path.basename(science_filename)}")

    # uncomment this once the relevant webbPSF PR is merged:
    #miri = webbpsf.setup_sim_to_match_file(science_filename, choice=choice, verbose=verbose)
    miri = webbpsf.setup_sim_to_match_file(science_filename, verbose=verbose)
    # Open the science file as a datamodel object
    sci_data = jwst.datamodels.open(science_filename)

    # create a mock data object, and set metadata appropriately for the WFS
    if verbose:
        print(f"*** Setting up a mock data object to match {science_filename}")

    mock_data = sci_data.copy()

    # extract metadata from the WFS sensing file, and use that to populate metadata for the sim
    wfs_full_obsid = miri.pupilopd[0].header['OBS_ID']
    wfs_progid = wfs_full_obsid[1:6]
    wfs_obs = wfs_full_obsid[6:9]
    wfs_vis = wfs_full_obsid[9:12]

    mock_data.meta.observation.program_number = wfs_progid
    mock_data.meta.observation.observation_number = wfs_obs
    mock_data.meta.observation.visit_number = wfs_vis
    mock_data.meta.observation.date = miri.pupilopd[0].header['DATE-OBS']
    mock_data.meta.observation.date_beg = miri.pupilopd[0].header['TSTAMP']
    mock_data.meta.target.proposer_name = 'Synthetic PSF from WFS'
    mock_data.meta.target.catalog_name = 'Synthetic PSF from WFS'
    mock_data.meta.exposure.psf_reference = True

    nints = mock_data.meta.exposure.nints

    if nints > 1:
        # there's no point to simulating multiple ints for a synthetic PSF...
        # TBD - this may require some more header hacking
        mock_data.meta.exposure.nints = 1
        nints = 1
        mock_data.data = mock_data.data[:1]   # Truncate to only one slice, but leave as a cube for format-compatibility
        mock_data.dq = mock_data.dq[:1]       # Truncate to only one slice, but leave as a cube for format-compatibility
        mock_data.err = mock_data.err[:1]     # Truncate to only one slice, but leave as a cube for format-compatibility

    mock_data.data[:] = 0   # Discard the original pixel values
    mock_data.err[:] = 0


    if miri.filter in ['F1065C', 'F1140C', 'F1550C']:
        npix_sim = int(np.round(24/miri.pixelscale))
        # offsets for coronagraph illumiated subregion within the full subarray. 
        # TO DO: get this better aligned. This is just an eyeball estimate
        coron_region_y0, coron_region_x0 = 3, 10
    else:
        npix_sim = int(np.round(30/miri.pixelscale))
        raise NotImplementedError("Lyot coron not yet implemented")


    #-- Setup small grid dither parameters for iteration
    if small_grid is None:
        dithers = ((0,0), )
    elif small_grid==5:
        dithers = spaceKLIP.constants.miri_small_grid_dither['5-POINT-SMALL-GRID']
    elif small_grid==9:
        dithers = spaceKLIP.constants.miri_small_grid_dither['9-POINT-SMALL-GRID']
    else:
        raise ValueError("Value for small_grid must be in [None, 5, 9]")

    outnames = []
    for idither, (offset_x, offset_y) in enumerate(dithers):
        exp_num = idither + 1

        if verbose:
            print(f"*** Simulating MIRI coronagraphic PSF for {miri.filter}")
            print(f"    Dither pos {exp_num} of {len(dithers)}. Offsets = {offset_x}, {offset_y} mas")

        miri.options['source_offset_x'] = offset_x / 1000
        miri.options['source_offset_y'] = offset_y / 1000

        psf = miri.calc_psf(fov_pixels=npix_sim, nlambda=nlambda)
        ext = 'DET_DIST'


        if verbose:
            print(f"*** Scaling and shifting simulated PSF to align with science data, approximately")
        # Stick the simulated PSF into the mock data object
        # set up indices:
        illuminated_region = np.s_[0, coron_region_y0:coron_region_y0+npix_sim, coron_region_x0:coron_region_x0+npix_sim]

        mock_data.data[illuminated_region]  = psf[ext].data

        mock_data.dq[:] = 1 # set all pixels to DO_NOT_USE by default
        mock_data.dq[illuminated_region] = 0  # these pixels have data


        #--- Figure out a flux scale factor, to make the sim PSF have roughly same counts as the science data.
        #  This shouldn't really matter for KLIP purposes, but is a convenience for displaying on similar stretches

        # Mask to good pixels in the science data, for setting flux ratio
        mask = (mock_data.dq & 1)[0] == 0  # extract "do not use bit", and get the GOOD pixels
        #mask2 = scipy.ndimage.morphology.binary_closing(mask, structure=np.ones((3,3)))  # discard isolated pixels; we just one the big shape

        wmask = np.where(mask)
        bglevel = np.nanmedian(sci_data.data[0][wmask])
        skysub_data = sci_data.data[0] - bglevel
        flux_scale_factor = np.nansum(skysub_data[wmask]) / np.nansum(mock_data.data[0][wmask])

        mock_data.data *= flux_scale_factor

        #--- Prepare to output
        filetype = sci_data.meta.filename.split('_')[-1]  # this will be "calints.fits" or "rate.fits" or the like

        outname = f'syn{mock_data.meta.observation.program_number}{mock_data.meta.observation.observation_number}{mock_data.meta.observation.visit_number}_'+\
                  f'{mock_data.meta.observation.visit_group}{mock_data.meta.observation.sequence_id}{mock_data.meta.observation.activity_id}_{exp_num:05d}_mirimage_{filetype}'

        if verbose:
            print(f"*** Saving output to {outname}")
        mock_data.write(outname)
        outnames.append(outname)

        if plot:
            spaceKLIP.plotting.display_coron_image(outname)

    return outnames
