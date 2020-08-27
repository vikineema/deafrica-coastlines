#!/usr/bin/env python
# coding: utf-8

# This code conducts vector subpixel coastline extraction for DEA 
# CoastLines:
# 
#     * Apply morphological extraction algorithms to mask annual median 
#       composite rasters to a valid coastal region
#     * Extract waterline vectors using subpixel waterline extraction 
#       (Bishop-Taylor et al. 2019b; https://doi.org/10.3390/rs11242984)
#     * Compute rates of coastal change at every 30 m along Australia's 
#       non-rocky coastlines using linear regression
#
# Compatability:
#
#     module use /g/data/v10/public/modules/modulefiles
#     module load dea/20200713
#     pip install --user ruptures
#     pip install --user git+https://github.com/mattijn/topojson/


import os
import sys
import glob
import numpy as np
import pandas as pd
import xarray as xr
import topojson as tp
import ruptures as rpt
import geopandas as gpd
from scipy import stats
from affine import Affine
from itertools import chain
from shapely.geometry import shape
from shapely.geometry import box
from shapely.geometry import LineString
from shapely.geometry import MultiLineString
from shapely.ops import nearest_points
from rasterio.features import shapes
from rasterio.features import sieve
from rasterio.features import rasterize
from rasterio.transform import array_bounds
from skimage.measure import label
from skimage.measure import find_contours
from skimage.morphology import binary_opening
from skimage.morphology import binary_erosion
from skimage.morphology import binary_dilation
from skimage.morphology import disk, square
from datacube.utils.cog import write_cog

import warnings
warnings.simplefilter(action='ignore', category=FutureWarning)

sys.path.append('/g/data/r78/rt1527/dea-notebooks/Scripts')
from dea_spatialtools import xr_vectorize

  
def load_rasters(output_name, 
                 study_area, 
                 water_index='mndwi'):
    
    """
    Loads DEA CoastLines water index (e.g. 'MNDWI'), 'tide_m', 'count',
    and 'stdev' rasters for both annual and three-year gapfill data
    into a consistent `xarray.Dataset` format for further analysis.
    
    Parameters:
    -----------
    output_name : string
        A string giving the unique DEA Coastlines analysis output name
        (e.g. 'v0.3.0') used to name raster files.
    study_area : string or int
        A string giving the study area used to name raster files 
        (e.g. Albers tile `6931`).
    water_index : string, optional
        A string giving the name of the water index to load. Defaults
        to 'mndwi', which will load raster files produced using the
        Modified Normalised Difference Water Index.

    Returns:
    --------
    yearly_ds : xarray.Dataset
        An `xarray.Dataset` containing annual DEA CoastLines rasters.
        The dataset contains water index (e.g. 'MNDWI'), 'tide_m', 
        'count', and 'stdev' arrays for each year from 1988 onward.
    gapfill_ds : xarray.Dataset
        An `xarray.Dataset` containing three-year gapfill DEA CoastLines
        rasters. The dataset contains water index (e.g. 'MNDWI'), 
        'tide_m', 'count', and 'stdev' arrays for each year from 1988 
        onward.
        
    """
    

    # List to hold output Datasets
    ds_list = []

    for layer_type in ['.tif', '_gapfill.tif']:

        # List to hold output DataArrays
        da_list = []

        for layer_name in [f'{water_index}', 'tide_m', 'count', 'stdev']:

            # Get paths of files that match pattern
            paths = glob.glob(f'output_data/{study_area}_{output_name}/' \
                              f'*_{layer_name}{layer_type}')

            # Test if data was returned
            if len(paths) == 0:
                raise ValueError(f"No rasters found for grid cell {study_area} "
                                 f"(analysis name '{output_name}'). Verify that "
                                 f"`deacoastlines_generation.py` has been run "
                                 "for this grid cell.")

            # Create variable used for time axis
            time_var = xr.Variable('year', 
                                   [int(i.split('/')[2][0:4]) for i in paths])

            # Import data
            layer_da = xr.concat([xr.open_rasterio(i) for i in paths], 
                                  dim=time_var)
            layer_da.name = f'{layer_name}'

            # Append to file
            da_list.append(layer_da)

        # Combine into a single dataset and set CRS
        layer_ds = xr.merge(da_list).squeeze('band', drop=True)
        layer_ds = layer_ds.assign_attrs(layer_da.attrs)
        layer_ds.attrs['transform'] = Affine(*layer_ds.transform)
        layer_ds = layer_ds.sel(year=slice(1988, None))

        # Append to list
        ds_list.append(layer_ds)
    
    return ds_list


def waterbody_mask(input_data,
                   modification_data,
                   bbox,
                   yearly_ds):
    """
    Generates a raster mask for DEACoastLines based on the 
    SurfaceHydrologyPolygonsRegional.gdb dataset, and a vector 
    file containing minor modifications to this dataset (e.g. 
    features to remove or add to the dataset).
    
    The mask returns True for perennial 'Lake' features, any 
    'Aquaculture Area', 'Estuary', 'Watercourse Area', 'Salt 
    Evaporator', and 'Settling Pond' features. Features of 
    type 'add' from the modification data file are added to the
    mask, while features of type 'remove' are removed.
    
    Parameters:
    -----------
    input_data : string
        A string giving the path to the file containing surface water
        polygons (e.g. SurfaceHydrologyPolygonsRegional.gdb)
    modification_data : string
        A string giving the path to a vector file containing 
        modifications to the waterbody file. This vector file should
        contain polygon features with an attribute field 'type'
        indicating whether the function should 'add' or 'remove' the 
        feature from the waterbody mask.
    bbox : geopandas.GeoSeries
        A `geopandas.GeoSeries` giving the spatial extent to load data 
        for. This object should include a CRS.
    yearly_ds : xr.Dataset
        The annual DEA CoastLines `xarray.Dataset`, used to extract the
        shape and geotransform so that waterbody features can be 
        rasterised into the data's extents.
        
    Returns:
    --------
    waterbody_mask : nd.array
        An array containing the rasterised surface water features.
    
    """

    # Import SurfaceHydrologyPolygonsRegional data
    waterbody_gdf = gpd.read_file(input_data, bbox=bbox).to_crs(yearly_ds.crs)

    # Restrict to coastal features
    lakes_bool = ((waterbody_gdf.FEATURETYPE == 'Lake') &
                  (waterbody_gdf.PERENNIALITY == 'Perennial'))
    other_bool = waterbody_gdf.FEATURETYPE.isin(['Aquaculture Area', 
                                                 'Estuary', 
                                                 'Watercourse Area', 
                                                 'Salt Evaporator', 
                                                 'Settling Pond'])
    waterbody_gdf = waterbody_gdf[lakes_bool | other_bool]

    # Load in modification dataset and select features to remove/add
    mod_gdf = gpd.read_file(modification_data, bbox=bbox).to_crs(yearly_ds.crs)
    to_remove = mod_gdf[mod_gdf['type'] == 'remove']
    to_add = mod_gdf[mod_gdf['type'] == 'add']

    # Remove and add features
    if len(to_remove.index) > 0:
        if len(waterbody_gdf.index) > 0:
            waterbody_gdf = gpd.overlay(waterbody_gdf, to_remove, how='difference')        
    if len(to_add.index) > 0:
        if len(waterbody_gdf.index) > 0:
            waterbody_gdf = gpd.overlay(waterbody_gdf, to_add, how='union')
        else:
            waterbody_gdf = to_add
        
    # Rasterize waterbody polygons into a numpy mask. The try-except catches 
    # cases where no waterbody polygons exist in the study area
    try:
        waterbody_mask = rasterize(shapes=waterbody_gdf['geometry'],
                                   out_shape=yearly_ds.geobox.shape,
                                   transform=yearly_ds.geobox.transform,
                                   all_touched=True).astype(bool)
    except:
        waterbody_mask = np.full(yearly_ds.geobox.shape, False, dtype=bool)
        
    return waterbody_mask


def mask_ocean(bool_array, tide_points_gdf, connectivity=1):
    """
    Identifies ocean by selecting the largest connected area of water
    pixels that contain tidal modelling points, then dilating this 
    region to ensure sub-pixel algorithm has pixels on either side of 
    the water index threshold.
    
    Parameters:
    -----------
    bool_array : xarray.DataArray
        An array containing True for water pixels, and False for non-
        water. This can be obtained by thresholding a water index
        array (e.g. MNDWI > 0).
    tide_points_gdf : geopandas.GeoDataFrame
        Spatial points located within the ocean. These points are used
        to ensure that all coastlines are directly connected to the 
        ocean.
    connectivity : integer, optional
        An integer passed to the 'connectivity' parameter of the
        `skimage.measure.label` function.
        
    Returns:
    --------
    ocean_mask : nd.array
        An array containing the a mask consisting of identified ocean 
        pixels.
    
    """
    
    # First, break boolean array into unique, discrete regions/blobs
    blobs_labels = xr.apply_ufunc(label, bool_array, None, 0, False, connectivity)
    
    # Get blob ID for each tidal modelling point
    x = xr.DataArray(tide_points_gdf.geometry.x, dims='z')
    y = xr.DataArray(tide_points_gdf.geometry.y, dims='z')   
    ocean_blobs = np.unique(blobs_labels.interp(x=x, y=y, method='nearest'))

    # Return only blobs that contained tide modelling point
    ocean_mask = blobs_labels.isin(ocean_blobs[ocean_blobs != 0])
    
    # Dilate mask so that we include land pixels on the inland side
    # of each shoreline to ensure contour extraction accurately
    # seperates land and water spectra
    ocean_mask = binary_dilation(ocean_mask, selem=square(4))

    return ocean_mask


def contours_preprocess(yearly_ds, 
                        gapfill_ds,
                        water_index, 
                        index_threshold, 
                        waterbody_array, 
                        tide_points_gdf,
                        output_path,
                        buffer_pixels=33):  
    """
    Prepares and preprocesses DEA CoastLines raster data to restrict the
    analysis to coastal shorelines, and extract data that is used to
    assess the certainty of extracted shorelines.
    
    This function:
    
    1) Identifies areas affected by either tidal issues, or low data
    2) Fills low data areas in annual layers with three-year gapfill
    3) Masks data to focus on ocean and coastal pixels only by removing
       any pixels not directly connected to ocean or included in an
       array of surface water (e.g. estuaries or inland waterbodies)
    4) Generate an overall coastal buffer using the entire timeseries,
       and clip each annual layer to this buffer
    5) Generate an all time mask raster containing data on tidal issues, 
       low data and coastal buffer to assist in interpreting results.
    
    Parameters:
    -----------
    yearly_ds : xarray.Dataset
        An `xarray.Dataset` containing annual DEA CoastLines rasters.
    gapfill_ds : xarray.Dataset
        An `xarray.Dataset` containing three-year gapfill DEA CoastLines
        rasters. 
    water_index : string
        A string giving the name of the water index included in the 
        annual and gapfill datasets (e.g. 'mndwi').
    index_threshold : float
        A float giving the water index threshold used to separate land
        and water (e.g. 0.00).
    waterbody_array : nd.array
        An array containing rasterised surface water features to exclude
        from the data, used by the `mask_ocean` function.
    tide_points_gdf : geopandas.GeoDataFrame
        Spatial points located within the ocean. These points are used
        by the `mask_ocean` to ensure that all coastlines are directly 
        connected to the ocean. These may be obtained from the tidal 
        modelling points used in the raster generation part of the DEA 
        CoastLines analysis, as these are guaranteed to be located in 
        coastal or marine waters.
    output_path : string
        A string giving the directory into which output all time mask 
        raster will be written.
    buffer_pixels : int, optional
        The number of pixels by which to buffer the all time shoreline
        detected by this function to produce an overall coastal buffer.
        The default is 33 pixels, which at 30 m Landsat resolution 
        produces a coastal buffer with a radius of approximately 1000 m.
        
    Returns:
    --------
    masked_ds : xarray.Dataset
        A dataset containing water index data for each annual timestep
        that has been masked to the coastal zone. This can then be used
        as an input to subpixel waterline extraction.
    
    """
    
    # Flag nodata pixels
    nodata = yearly_ds[water_index].isnull()
    
    # Identify pixels with less than 5 annual observations or > 0.25 
    # MNDWI standard deviation in more than half the time series.
    # Apply binary erosion to isolate large connected areas of 
    # problematic pixels
    mean_stdev = (yearly_ds['stdev'] > 0.25).where(~nodata).mean(dim='year')
    mean_count = (yearly_ds['count'] < 5).where(~nodata).mean(dim='year')
    persistent_stdev = binary_erosion(mean_stdev > 0.5, selem=disk(2))
    persistent_lowobs = binary_erosion(mean_count > 0.5, selem=disk(2))

    # Remove low obs pixels and replace with 3-year gapfill
    yearly_ds = yearly_ds.where(yearly_ds['count'] > 5, gapfill_ds)
    
    # Update nodata layer based on gap-filled data
    nodata = yearly_ds[water_index].isnull()
    
    # Apply water index threshold, restore nodata values back to NaN, 
    # and assign pixels within waterbody mask to 0 so they are excluded
    thresholded_ds = ((yearly_ds[water_index] > index_threshold)
                      .where(~nodata).where(~waterbody_array, 0))
    
    # Identify ocean by identifying the largest connected area of water pixels
    # as water in at least 90% of the entire stack of thresholded data.
    # Apply a binary opening step to clean noisy pixels
    all_time = thresholded_ds.mean(dim='year') > 0.9
    all_time_cleaned = xr.apply_ufunc(binary_opening, all_time, disk(3))
    all_time_ocean = mask_ocean(all_time_cleaned, tide_points_gdf)   
    
    # Generate coastal buffer (30m * `buffer_pixels`) from ocean-land boundary
    buffer_ocean = binary_dilation(all_time_ocean, disk(buffer_pixels))
    buffer_land = binary_dilation(~all_time_ocean, disk(buffer_pixels))
    coastal_buffer = buffer_ocean & buffer_land
    
    # Generate annual masks by selecting only water pixels that are 
    # directly connected to the ocean in each yearly timestep
    annual_masks = (thresholded_ds.groupby('year')
                    .apply(lambda x: mask_ocean(x, tide_points_gdf)))

    # Keep pixels within both all time coastal buffer and annual mask
    masked_ds = yearly_ds[water_index].where(annual_masks & coastal_buffer)
    
    # Create raster containg all time mask data
    all_time_mask = np.full(yearly_ds.geobox.shape, 0, dtype='int8')
    all_time_mask[buffer_land & ~coastal_buffer] = 1
    all_time_mask[buffer_ocean & ~coastal_buffer] = 2
    all_time_mask[waterbody_array & coastal_buffer] = 3
    all_time_mask[persistent_stdev & coastal_buffer] = 4
    all_time_mask[persistent_lowobs & coastal_buffer] = 5

    # Export mask raster to assist evaluating results
    all_time_mask_da = xr.DataArray(data = all_time_mask, 
                                    coords={'x': yearly_ds.x, 
                                            'y': yearly_ds.y},
                                    dims=['y', 'x'],
                                    name='all_time_mask',
                                    attrs=yearly_ds.attrs)
    write_cog(geo_im=all_time_mask_da, 
              fname=f'{output_path}/all_time_mask.tif', 
              blocksize=256, 
              overwrite=True)
    
    # Reset attributes and return data
    masked_ds.attrs = yearly_ds.attrs

    return masked_ds


def subpixel_contours(da,
                      z_values=[0.0],
                      crs=None,
                      affine=None,
                      attribute_df=None,
                      output_path=None,
                      min_vertices=2,
                      dim='time',
                      errors='ignore'):
    
    """
    Uses `skimage.measure.find_contours` to extract multiple z-value 
    contour lines from a two-dimensional array (e.g. multiple elevations
    from a single DEM), or one z-value for each array along a specified 
    dimension of a multi-dimensional array (e.g. to map waterlines 
    across time by extracting a 0 NDWI contour from each individual 
    timestep in an xarray timeseries).    
    
    Contours are returned as a geopandas.GeoDataFrame with one row per 
    z-value or one row per array along a specified dimension. The 
    `attribute_df` parameter can be used to pass custom attributes 
    to the output contour features.
    
    Last modified: June 2020
    
    Parameters
    ----------  
    da : xarray DataArray
        A two-dimensional or multi-dimensional array from which 
        contours are extracted. If a two-dimensional array is provided, 
        the analysis will run in 'single array, multiple z-values' mode 
        which allows you to specify multiple `z_values` to be extracted.
        If a multi-dimensional array is provided, the analysis will run 
        in 'single z-value, multiple arrays' mode allowing you to 
        extract contours for each array along the dimension specified 
        by the `dim` parameter.  
    z_values : int, float or list of ints, floats
        An individual z-value or list of multiple z-values to extract 
        from the array. If operating in 'single z-value, multiple 
        arrays' mode specify only a single z-value.
    crs : string or CRS object, optional
        An EPSG string giving the coordinate system of the array 
        (e.g. 'EPSG:3577'). If none is provided, the function will 
        attempt to extract a CRS from the xarray object's `crs` 
        attribute.
    affine : affine.Affine object, optional
        An affine.Affine object (e.g. `from affine import Affine; 
        Affine(30.0, 0.0, 548040.0, 0.0, -30.0, "6886890.0) giving the 
        affine transformation used to convert raster coordinates 
        (e.g. [0, 0]) to geographic coordinates. If none is provided, 
        the function will attempt to obtain an affine transformation 
        from the xarray object (e.g. either at `da.transform` or
        `da.geobox.transform`).
    output_path : string, optional
        The path and filename for the output shapefile.
    attribute_df : pandas.Dataframe, optional
        A pandas.Dataframe containing attributes to pass to the output
        contour features. The dataframe must contain either the same 
        number of rows as supplied `z_values` (in 'multiple z-value, 
        single array' mode), or the same number of rows as the number 
        of arrays along the `dim` dimension ('single z-value, multiple 
        arrays mode').
    min_vertices : int, optional
        The minimum number of vertices required for a contour to be 
        extracted. The default (and minimum) value is 2, which is the 
        smallest number required to produce a contour line (i.e. a start
        and end point). Higher values remove smaller contours, 
        potentially removing noise from the output dataset.
    dim : string, optional
        The name of the dimension along which to extract contours when 
        operating in 'single z-value, multiple arrays' mode. The default
        is 'time', which extracts contours for each array along the time
        dimension.
    errors : string, optional
        If 'raise', then any failed contours will raise an exception.
        If 'ignore' (the default), a list of failed contours will be
        printed. If no contours are returned, an exception will always
        be raised.
        
    Returns
    -------
    output_gdf : geopandas geodataframe
        A geopandas geodataframe object with one feature per z-value 
        ('single array, multiple z-values' mode), or one row per array 
        along the dimension specified by the `dim` parameter ('single 
        z-value, multiple arrays' mode). If `attribute_df` was 
        provided, these values will be included in the shapefile's 
        attribute table.
    """

    def contours_to_multiline(da_i, z_value, min_vertices=2):
        '''
        Helper function to apply marching squares contour extraction
        to an array and return a data as a shapely MultiLineString.
        The `min_vertices` parameter allows you to drop small contours 
        with less than X vertices.
        '''
        
        # Extracts contours from array, and converts each discrete
        # contour into a Shapely LineString feature

        try:
            line_features = [LineString(i[:,[1, 0]]) 
                             for i in find_contours(da_i.data, z_value) 
                             if i.shape[0] > min_vertices]
            
        except:
            line_features = [LineString(i[:,[1, 0]]) 
                             for i in find_contours(da_i.data, z_value, 
                                                    fully_connected='high') 
                             if i.shape[0] > min_vertices]          

        # Output resulting lines into a single combined MultiLineString
        return MultiLineString(line_features)

    # Check if CRS is provided as a xarray.DataArray attribute.
    # If not, require supplied CRS
    try:
        crs = da.crs
    except:
        if crs is None:
            raise Exception("Please add a `crs` attribute to the "
                            "xarray.DataArray, or provide a CRS using the "
                            "function's `crs` parameter (e.g. 'EPSG:3577')")

    # Check if Affine transform is provided as a xarray.DataArray method.
    # If not, require supplied Affine
    try:
        affine = da.geobox.transform
    except KeyError:
        affine = da.transform
    except:
        if affine is None:
            raise Exception("Please provide an Affine object using the "
                            "`affine` parameter (e.g. `from affine import "
                            "Affine; Affine(30.0, 0.0, 548040.0, 0.0, -30.0, "
                            "6886890.0)`")

    # If z_values is supplied is not a list, convert to list:
    z_values = z_values if (isinstance(z_values, list) or 
                            isinstance(z_values, np.ndarray)) else [z_values]

    # Test number of dimensions in supplied data array
    if len(da.shape) == 2:

        print(f'Operating in multiple z-value, single array mode')
        dim = 'z_value'
        contour_arrays = {str(i)[0:10]: 
                          contours_to_multiline(da, i, min_vertices) 
                          for i in z_values}    

    else:

        # Test if only a single z-value is given when operating in 
        # single z-value, multiple arrays mode
        print(f'Operating in single z-value, multiple arrays mode')
        if len(z_values) > 1:
            raise Exception('Please provide a single z-value when operating '
                            'in single z-value, multiple arrays mode')

        contour_arrays = {str(i)[0:10]: 
                          contours_to_multiline(da_i, z_values[0], min_vertices) 
                          for i, da_i in da.groupby(dim)}

    # If attributes are provided, add the contour keys to that dataframe
    if attribute_df is not None:

        try:
            attribute_df.insert(0, dim, contour_arrays.keys())
        except ValueError:

            raise Exception("One of the following issues occured:\n\n"
                            "1) `attribute_df` contains a different number of "
                            "rows than the number of supplied `z_values` ("
                            "'multiple z-value, single array mode')\n"
                            "2) `attribute_df` contains a different number of "
                            "rows than the number of arrays along the `dim` "
                            "dimension ('single z-value, multiple arrays mode')")

    # Otherwise, use the contour keys as the only main attributes
    else:
        attribute_df = list(contour_arrays.keys())

    # Convert output contours to a geopandas.GeoDataFrame
    contours_gdf = gpd.GeoDataFrame(data=attribute_df, 
                                    geometry=list(contour_arrays.values()),
                                    crs=crs)   

    # Define affine and use to convert array coords to geographic coords.
    # We need to add 0.5 x pixel size to the x and y to obtain the centre 
    # point of our pixels, rather than the top-left corner
    shapely_affine = [affine.a, affine.b, affine.d, affine.e, 
                      affine.xoff + affine.a / 2.0, 
                      affine.yoff + affine.e / 2.0]
    contours_gdf['geometry'] = contours_gdf.affine_transform(shapely_affine)

    # Rename the data column to match the dimension
    contours_gdf = contours_gdf.rename({0: dim}, axis=1)

    # Drop empty timesteps
    empty_contours = contours_gdf.geometry.is_empty
    failed = ', '.join(map(str, contours_gdf[empty_contours][dim].to_list()))
    contours_gdf = contours_gdf[~empty_contours]

    # Raise exception if no data is returned, or if any contours fail
    # when `errors='raise'. Otherwise, print failed contours
    if empty_contours.all():
        raise Exception("Failed to generate any valid contours; verify that "
                        "values passed to `z_values` are valid and present "
                        "in `da`")
    elif empty_contours.any() and errors == 'raise':
        raise Exception(f'Failed to generate contours: {failed}')
    elif empty_contours.any() and errors == 'ignore':
        print(f'Failed to generate contours: {failed}')

    # If asked to write out file, test if geojson or shapefile
    if output_path and output_path.endswith('.geojson'):
        print(f'Writing contours to {output_path}')
        contours_gdf.to_crs({'init': 'EPSG:4326'}).to_file(filename=output_path, 
                                                           driver='GeoJSON')
    if output_path and output_path.endswith('.shp'):
        print(f'Writing contours to {output_path}')
        contours_gdf.to_file(filename=output_path)
        
    return contours_gdf


def points_on_line(gdf, index, distance=30):    
    """
    Generates evenly-spaced point features along a specific line feature
    in a `geopandas.GeoDataFrame`.
    
    Parameters:
    -----------
    gdf : geopandas.GeoDataFrame
        A `geopandas.GeoDataFrame` containing line features with an 
        index and CRS.
    index : string or int
        An value giving the index of the line to generate points along
    distance : integer or float, optional
        A number giving the interval at which to generate points along 
        the line feature. Defaults to 30, which will generate a point
        at every 30 metres along the line.
        
    Returns:
    --------
    points_gdf : geopandas.GeoDataFrame
        A `geopandas.GeoDataFrame` containing point features at every
        `distance` along the selected line.
    
    """
    
    # Select individual line to generate points along
    line_feature = gdf.loc[[index]].geometry    
    
    # If multiple features are returned, take unary union
    if line_feature.shape[0] > 0:
        line_feature = line_feature.unary_union
    else:
        line_feature = line_feature.iloc[0]

    # Generate points along line and convert to geopandas.GeoDataFrame
    points_line = [line_feature.interpolate(i) 
                   for i in range(0, int(line_feature.length), distance)]
    points_gdf = gpd.GeoDataFrame(geometry=points_line, crs=gdf.crs)
    
    return points_gdf


def rocky_shores_clip(points_gdf, smartline_gdf, buffer=50):
    """
    Clips rates of change points to a buffer around non-rocky (clastic)
    coastlines based on the Smartline dataset.
    
    This processing step aims to be conservative, and preserves any
    unclassified points or any points that occur next to a non-rocky 
    shoreline in either the 'INTERTD1_V' or 'INTERTD2_V' Smartline 
    fields.    
    
    Parameters:
    -----------
    points_gdf : geopandas.GeoDataFrame
        A `geopandas.GeoDataFrame` containing rates of change points.
    smartline_gdf : geopandas.GeoDataFrame
        A `geopandas.GeoDataFrame` containing SmartLine data for the
        study area.
    buffer : integer or float, optional
        A number giving the buffer around non-rocky (clastic) shorelines
        within which to clip rates of change points. Defaults to 50 m.
        
    Returns:
    --------
    points_gdf : geopandas.GeoDataFrame
        A `geopandas.GeoDataFrame` containing rates of change points
        restricted to non-rocky (clastic) coastlines.    
    """

    rocky = [
               'Bedrock breakdown debris (cobbles/boulders)',
               'Boulder (rock) beach',
               'Cliff (>5m) (undiff)',
               'Colluvium (talus) undiff',
               'Flat boulder deposit (rock) undiff',
               'Hard bedrock shore',
               'Hard bedrock shore inferred',
               'Hard rock cliff (>5m)',
               'Hard rocky shore platform',
               'Rocky shore (undiff)',
               'Rocky shore platform (undiff)',
               'Sloping hard rock shore',
               'Sloping rocky shore (undiff)',
               'Soft `bedrock¿ cliff (>5m)',
               'Steep boulder talus',
               'Hard rocky shore platform'
    ]
    
    # Identify rocky features
    rocky_bool = (smartline_gdf.INTERTD1_V.isin(rocky) & 
                  smartline_gdf.INTERTD2_V.isin(rocky + ['Unclassified']))

    # Extract rocky vs non-rocky
    rocky_gdf = smartline_gdf[rocky_bool].copy()
    nonrocky_gdf = smartline_gdf[~rocky_bool].copy()

    # If both rocky and non-rocky shorelines exist, clip points to remove
    # rocky shorelines from the stats dataset
    if (len(rocky_gdf) > 0) & (len(nonrocky_gdf) > 0):

        # Buffer both features
        rocky_gdf['geometry'] = rocky_gdf.buffer(buffer)
        nonrocky_gdf['geometry'] = nonrocky_gdf.buffer(buffer)
        rocky_shore_buffer = (gpd.overlay(rocky_gdf, 
                                          nonrocky_gdf, 
                                          how='difference')
                              .geometry
                              .unary_union)
        
        # Keep only non-rocky shore features and reset index         
        points_gdf = points_gdf[~points_gdf.intersects(rocky_shore_buffer)]        
        points_gdf = points_gdf.reset_index(drop=True)        
        
        return points_gdf

    # If no rocky shorelines exist, return the points data as-is
    elif len(nonrocky_gdf) > 0:          
        return points_gdf
   
    # If no sandy shorelines exist, return nothing
    else:
        return None

    
def annual_movements(points_gdf,
                     contours_gdf,
                     yearly_ds,                     
                     baseline_year, 
                     water_index):
    """
    For each rate of change point along the baseline annual coastline, 
    compute the distance to the nearest point on all neighbouring annual
    coastlines and add this data as new fields in the dataset.
    
    Distances are assigned a directionality (negative = located inland, 
    positive = located sea-ward) by sampling water index values from the 
    underlying DEA CoastLines rasters to determine if a coastline was 
    located in wetter or drier terrain than the baseline coastline.
    
    Parameters:
    -----------
    points_gdf : geopandas.GeoDataFrame
        A `geopandas.GeoDataFrame` containing rates of change points.
    contours_gdf : geopandas.GeoDataFrame
        A `geopandas.GeoDataFrame` containing annual coastlines.
    yearly_ds : xarray.Dataset
        An `xarray.Dataset` containing annual DEA CoastLines rasters.
    baseline_year : string
        A string giving the year used as the baseline when generating 
        the rates of change points dataset. This is used to load DEA
        CoastLines water index rasters to calculate change 
        directionality.
    water_index : string
        A string giving the water index used in the analysis. This is 
        used to load DEA CoastLines water index rasters to calculate 
        change directionality.
        
    Returns:
    --------
    points_gdf : geopandas.GeoDataFrame
        A `geopandas.GeoDataFrame` containing rates of change points
        with added 'dist_*' attribute columns giving the distance to
        each annual coastline from the baseline. Negative values
        indicate that an annual coastline was located inland of the 
        baseline; positive values indicate the coastline was located 
        towards the ocean.
    """

    # Get array of water index values for baseline time period 
    baseline_array = yearly_ds[water_index].sel(year=int(baseline_year))

    # Copy baseline point geometry to new column in points dataset
    points_gdf['p_baseline'] = points_gdf.geometry
    baseline_x_vals = points_gdf.geometry.x
    baseline_y_vals = points_gdf.geometry.y

    # Years to analyse
    years = contours_gdf.index.unique().values

    # Iterate through all comparison years in contour gdf
    for comp_year in years:

        print(comp_year, end='\r')

        # Set comparison contour
        comp_contour = contours_gdf.loc[[comp_year]].geometry.iloc[0]

        # Find nearest point on comparison contour, and add these to points dataset
        points_gdf[f'p_{comp_year}'] = points_gdf.apply(lambda x: 
                                                        nearest_points(x.p_baseline, 
                                                                       comp_contour)[1], 
                                                        axis=1)

        # Compute distance between baseline and comparison year points and add
        # this distance as a new field named by the current year being analysed
        distances = points_gdf.apply(
            lambda x: x.geometry.distance(x[f'p_{comp_year}']), axis=1)
        
        # Set any value over 1000 m to NaN
        points_gdf[f'dist_{comp_year}'] = distances.where(distances < 1000)

        # Extract comparison array containing water index values for the 
        # current year being analysed
        comp_array = yearly_ds[water_index].sel(year=int(comp_year))

        # Convert baseline and comparison year points to geoseries to allow 
        # easy access to x and y coords
        comp_x_vals = gpd.GeoSeries(points_gdf[f'p_{comp_year}']).x
        comp_y_vals = gpd.GeoSeries(points_gdf[f'p_{comp_year}']).y

        # Sample water index values from arrays for baseline and comparison points
        baseline_x_vals = xr.DataArray(baseline_x_vals, dims='z')
        baseline_y_vals = xr.DataArray(baseline_y_vals, dims='z')
        comp_x_vals = xr.DataArray(comp_x_vals, dims='z')
        comp_y_vals = xr.DataArray(comp_y_vals, dims='z')   
        points_gdf['index_comp_p1'] = comp_array.interp(x=baseline_x_vals, 
                                                        y=baseline_y_vals)
        points_gdf['index_baseline_p2'] = baseline_array.interp(x=comp_x_vals, 
                                                                y=comp_y_vals)

        # Compute change directionality (negative = located inland, 
        # positive = located towards the ocean)    
        points_gdf['loss_gain'] = np.where(points_gdf.index_baseline_p2 > 
                                           points_gdf.index_comp_p1, 1, -1)
        points_gdf[f'dist_{comp_year}'] = (points_gdf[f'dist_{comp_year}'] * 
                                           points_gdf.loss_gain)
  
    # Keep required columns
    to_keep = points_gdf.columns.str.contains('dist|geometry')
    points_gdf = points_gdf.loc[:, to_keep]
    points_gdf = points_gdf.assign(**{f'dist_{baseline_year}': 0.0})
    points_gdf = points_gdf.round(2)    
    
    return points_gdf


def outlier_mad(points, thresh=3.5):
    """
    Use robust Median Absolute Deviation (MAD) outlier detection 
    algorithm to detect outliers. Returns a boolean array with True if 
    points are outliers and False otherwise.

    Parameters:
    -----------
    points : 
        An n-observations by n-dimensions array of observations
    thresh : 
        The modified z-score to use as a threshold. Observations with a 
        modified z-score (based on the median absolute deviation) greater
        than this value will be classified as outliers.

    Returns:
    --------
    mask : 
        A n-observations-length boolean array.

    References:
    ----------
    Source: https://github.com/joferkington/oost_paper_code/blob/master/utilities.py
    
    Boris Iglewicz and David Hoaglin (1993), "Volume 16: How to Detect and
    Handle Outliers", The ASQC Basic References in Quality Control:
    Statistical Techniques, Edward F. Mykytka, Ph.D., Editor. 
    """
    if len(points.shape) == 1:
        points = points[:,None]
    median = np.median(points, axis=0)
    diff = np.sum((points - median)**2, axis=-1)
    diff = np.sqrt(diff)
    med_abs_deviation = np.median(diff)

    modified_z_score = 0.6745 * diff / med_abs_deviation

    return modified_z_score > thresh


def change_regress(row, 
                   x_vals, 
                   x_labels, 
                   threshold=3.5,
                   detrend_params=None,
                   slope_var='slope', 
                   interc_var='intercept',
                   pvalue_var='pvalue', 
                   stderr_var='stderr',
                   outliers_var='outliers'):
    """
    For a given row in a `pandas.DataFrame`, apply linear regression to
    data values (as y-values) and a corresponding sequence of x-values, 
    and return 'slope', 'intercept', 'pvalue', and 'stderr' regression
    parameters.
    
    Before computing the regression, outliers are identified using a
    robust Median Absolute Deviation (MAD) outlier detection algorithm,
    and excluded from the regression. A list of these outliers will be
    recorded in the output 'outliers' variable.

    Parameters:
    -----------
    row : 
        A `pandas.DataFrame` row
    x_vals : list of numeric values, or nd.array
        A sequence of values to use as the x/independent variable
    x_labels : list
        A sequence of strings corresponding to each value in `x_vals`.
        This is used to label any observations that are flagged as 
        outliers (often, this can simply be set to the same list 
        provided to `x_vals`).
    threshold : float, optional    
        The modified z-score to use as a threshold for detecting 
        outliers using the MAD algorithm. Observations with a modified 
        z-score (based on the median absolute deviation) greater
        than this value will be classified as outliers.
    detrend_params : optional
        Not currently used
    slope, interc_var, pvalue_var, stderr_var : strings, optional
        Strings giving the names to use for each of the output 
        regression variables.    
    outliers_var : string, optional
        String giving the name to use for the output outlier variable.        

    Returns:
    --------
    mask : 
        A `pandas.Series` containing regression parameters and lists
        of outliers.
    
    """
    
    # Extract x (time) and y (distance) values
    x = x_vals
    y = row.values.astype(np.float)
    
    # Drop NAN rows
    xy_df = np.vstack([x, y]).T
    is_valid = ~np.isnan(xy_df).any(axis=1)
    xy_df = xy_df[is_valid]
    valid_labels = x_labels[is_valid]
    
    # If detrending parameters are provided, apply these to the data to
    # remove the trend prior to running the regression
    if detrend_params:
        xy_df[:,1] = xy_df[:,1]-(detrend_params[0]*xy_df[:,0]+detrend_params[1])    
    
    # Remove outliers
    outlier_bool = ~outlier_mad(xy_df, thresh=threshold)
    xy_df = xy_df[outlier_bool]
        
    # Compute linear regression
    lin_reg = stats.linregress(x=xy_df[:,0], 
                               y=xy_df[:,1])  
    
    # Return slope, p-values and list of outlier years excluded from regression   
    results_dict = {slope_var: np.round(lin_reg.slope, 3), 
                    interc_var: np.round(lin_reg.intercept, 3),
                    pvalue_var: np.round(lin_reg.pvalue, 3),
                    stderr_var: np.round(lin_reg.stderr, 3),
                    outliers_var: ' '.join(map(str, valid_labels[~outlier_bool]))}
    
    return pd.Series(results_dict)


def calculate_regressions(points_gdf,
                          contours_gdf, 
                          climate_df):
    """
    For each rate of change point along the baseline annual coastline, 
    compute linear regression rates of change against both time and
    climate indices.
    
    Regressions are computed after removing outliers to ensure robust
    results.
    
    Parameters:
    -----------
    points_gdf : geopandas.GeoDataFrame
        A `geopandas.GeoDataFrame` containing rates of change points 
        with 'dist_*' annual movement/distance data.
    contours_gdf : geopandas.GeoDataFrame
        A `geopandas.GeoDataFrame` containing annual coastlines. This
        is used to ensure that all years in the annual coastlines data
        are included in the regression.
    climate_df : pandas.DataFrame
        A dataframe including numeric climate index data for each year
        in the input `contours_gdf` dataset.
        
    Returns:
    --------
    points_gdf : geopandas.GeoDataFrame
        A `geopandas.GeoDataFrame` containing rates of change points
        with additional attribute columns:
        
            'rate_*':  Slope of the regression 
            'sig_*':   Significance of the regression
            'se_*':    Standard error of the  regression
            'outl_*':  A list of any outlier years excluded from the 
                       regression        
    """

    # Restrict climate and points data to years in datasets
    x_years = contours_gdf.index.unique().astype(int).values
    dist_years = [f'dist_{i}' for i in x_years]  
    points_subset = points_gdf[dist_years]
    climate_subset = climate_df.loc[x_years, :]

    # Compute coastal change rates by linearly regressing annual movements vs. time
    print(f'Comparing annual movements with time')
    rate_out = (points_subset
                .apply(lambda x: change_regress(row=x,
                                                x_vals=x_years,
                                                x_labels=x_years), axis=1))
    points_gdf[['rate_time', 'incpt_time', 'sig_time', 'se_time', 'outl_time']] = rate_out

    # Identify possible relationships between climate indices and coastal change 
    # by linearly regressing climate indices against annual movements. Significant 
    # results indicate that annual movements may be influenced by climate phenomena
    for ci in climate_subset:

        print(f'Comparing annual movements with {ci}')

        # Compute stats for each row
        ci_out = (points_subset
                  .apply(lambda x: change_regress(row=x, 
                                                  x_vals=climate_subset[ci].values, 
                                                  x_labels=x_years), axis=1))

        # Add data as columns  
        points_gdf[[f'rate_{ci}', f'incpt_{ci}', f'sig_{ci}', 
                    f'se_{ci}', f'outl_{ci}']] = ci_out

    # Set CRS
    points_gdf.crs = contours_gdf.crs
    
    # Custom sorting
    reg_cols = chain.from_iterable([f'rate_{i}', f'sig_{i}', 
                                    f'se_{i}', f'outl_{i}'] for i in 
                                   ['time', *climate_df.columns]) 
    
    return points_gdf.loc[:, [*reg_cols, *dist_years, 'geometry']]


def breakpoints(x, labels, model='l1', pen=200, min_size=2, jump=1):
    """
    Takes an array of values, and returns a labelled breakpoints list
    using the `ruptures` Python package.
    
    Parameters:
    -----------
    x : array-like
        An array of numeric values used as the input to the breakpoint
        detection algorithm.
    labels : array-like
        An array of labels corresponding to each item in `x`, used to
        return a labelled list of outliers.
    pen : integer, optional
        Penalty value used to detect outliers, passed to `ruptures`'
        `.predict` method.
    min_size : integer, optional
        Minimum segment length used to detect outliers, passed to 
        `ruptures`' `Pelt` function.
    jump : integer, optional
         Subsampling (e.g. one every jump points) used to detect 
         outliers, passed to `ruptures`' `Pelt` function.
        
    Returns:
    --------
    A list containing the label of any observation that was detected
    as a breakpoint value.
    
    Notes:
    -----------
    For more information on the parameters above, see:
    https://centre-borelli.github.io/ruptures-docs/detection/pelt.html
    """
    
    algo = rpt.Pelt(model=model, min_size=min_size, jump=jump).fit(x)
    result = algo.predict(pen=pen)
    return [labels[i] for i in result[0:-1]]


def all_time_stats(x, col='dist_'):
    """
    Apply any statistics that apply to the entire set of annual 
    distance/movement values. This currently includes:
    
        sce: Shoreline Change Envelope (SCE). A measure of the maximum 
             change or variability across all annual coastlines, 
             calculated by computing the maximum distance between any 
             two annual coastlines (excluding outliers).
        nsm: Net Shoreline Movement (NSM). The distance between the 
             oldest (1988) and most recent (2019) annual coastlines 
             (excluding outliers). Negative values indicate the 
             shoreline retreated between the oldest and most recent 
             coastline; positive values indicate growth.
        max_year, min_year: The year that annual coastlines were at 
             their maximum (i.e. located furthest towards the ocean) and
             their minimum (i.e. located furthest inland) respectively 
             (excluding outliers). 
    
    Parameters:
    -----------
    x : pandas.DataFrame row
        A single row of the annual rates of change `pandas.DataFrame`
        containg columns of annual distances from the baseline.
    col : string, optional
        A string giving the prefix used for all annual distance/
        movement values. The default is 'dist_'.
        
    Returns:
    --------
    A `pandas.Series` containing new all time statistics.
    """

    # Select date columns only
    to_keep = x.index.str.contains(col)
    
    # Identify outlier years to drop from calculation
    to_drop = [f'{col}{i}' for i in x.outl_time.split(" ") if len(i) > 0]
    
    # Return matching subset of data
    subset_outl = x.loc[to_keep].dropna().astype(float) 
    subset_nooutl = subset_outl.drop(to_drop) 

    # Calculate SCE range, NSM and max/min year 
    # Since NSM is the most recent shoreline minus the oldest shoreline,
    # we can calculate this by simply inverting the 1988 distance value
    # (i.e. 0 - X) if it exists in the data
    stats_dict = {'sce': subset_nooutl.max() - subset_nooutl.min(),
                  'nsm': -(subset_nooutl.loc[f'{col}1988'] if 
                          f'{col}1988' in subset_nooutl else np.nan),
                  'max_year': int(subset_nooutl.idxmax()[-4:]),
                  'min_year': int(subset_nooutl.idxmin()[-4:])}

    # Compute breaks
    breaks = breakpoints(x=subset_outl.values, 
                         labels=subset_outl.index.str.slice(5))
    stats_dict.update({'breaks': ' '.join(breaks)})
    
    return pd.Series(stats_dict)


def contour_certainty(contours_gdf, 
                      output_path, 
                      uncertain_classes=[4, 5]):
    
    """
    Assigns a new certainty column to each annual shoreline feature
    based on two factors:
    
    1) Low satellite observations: pixels with less than 5 annual 
       observations for more than half of the time series.
    2) Tidal modelling issues: MNDWI standard deviation > 0.25 in more 
       than half of the time series.
    3) 1991 and 1992 coastlines affected by aerosol issues associated 
       with the 1991 eruption of Mt Pinatubo
    
    Parameters:
    -----------
    contours_gdf : geopandas.GeoDataFrame
        A `geopandas.GeoDataFrame` containing annual coastlines. This
        is used to ensure that all years in the annual coastlines data
        are included in the regression.
    output_path : string
        A string giving the directory where the 'all_time_mask.tif' file 
        was generated by the `contours_preprocess` function.
    uncertain_classes : list, optional
        A list of integers giving the classes in the 'all_time_mask.tif'
        to treat as uncertain (e.g. low satellite observations and tidal
        modelling issues).
        
    Returns:
    --------
    contours_gdf : geopandas.GeoDataFrame
        A `geopandas.GeoDataFrame` of annual coastlines with a new 
        'certainty' column.
    """
    
    def _extract_multiline(row):

        if row.geometry.type == 'GeometryCollection':
            lines = [g for g in row.geometry.geoms if g.type == 'LineString']
            return MultiLineString(lines)
        else:
            return row.geometry

    # Read data and restrict to uncertain vs certain classes
    all_time_mask = xr.open_rasterio(f'{output_path}/all_time_mask.tif')
    uncertain_array = all_time_mask.squeeze().drop('band').data.astype(np.int32)
    uncertain_array[~np.isin(uncertain_array, uncertain_classes)] = 0

    # Remove isolated pixels and vectorise data
    uncertain_array = sieve(uncertain_array, size=3)
    vectors = shapes(source=uncertain_array,
                     transform=all_time_mask.geobox.transform)

    # Extract the polygon coordinates and values from the list
    vectors = list(vectors)
    polygons = [shape(polygon) for polygon, value in vectors]
    values = [int(value) for polygon, value in vectors]

    # Create a geopandas dataframe populated with the polygon shapes
    vector_mask = gpd.GeoDataFrame(data={'certainty': values},
                                   geometry=polygons,
                                   crs=all_time_mask.geobox.crs)

    # Dissolve by class and simplify features to remove hard pixel edges
    topo = tp.Topology(vector_mask, shared_coords=True, prequantize=False)
    vector_mask = topo.toposimplify(30).to_gdf()
    vector_mask = vector_mask.dissolve('certainty')
    vector_mask['geometry'] = vector_mask.geometry.buffer(0)

    # Rename classes
    vector_mask = vector_mask.rename({0: 'good', 
                                      4: 'tidal issues', 
                                      5: 'insufficient data'})    

    # Output class list
    class_list = []

    # Iterate through each certainty class in the polygon, clip contours
    # to the extent of this class, and assign descriptive class name
    for i in vector_mask.index:

        # Clip to extent and fix invalid GeometryCollections
        vector_class = gpd.clip(contours_gdf, vector_mask.loc[i].geometry)
        vector_class = vector_class.dropna()
        
        if len(vector_class.index) > 0:
            vector_class['geometry'] = gpd.GeoSeries(
                vector_class.apply(_extract_multiline, axis=1))

            # Give name and append to list
            vector_class['certainty'] = i
            class_list.append(vector_class)

    # Combine into a single dataframe
    contours_gdf = pd.concat(class_list)
    
    # Finally, set all 1991 and 1992 coastlines north of -23 degrees 
    # latitude to 'uncertain' due to Mt Pinatubo aerosol issue
    pinatubo_lat = ((contours_gdf.centroid.to_crs('EPSG:4326').y > -23) & 
                    (contours_gdf.index.isin(['1991', '1992'])))
    contours_gdf.loc[pinatubo_lat, 'certainty'] = 'aerosol issues'
    
    return contours_gdf

    
def main(argv=None):
    
    #########
    # Setup #
    #########

    if argv is None:

        argv = sys.argv
        print(sys.argv)

    # If no user arguments provided
    if len(argv) < 3:

        str_usage = "You must specify a study area ID and name"
        print(str_usage)
        sys.exit()
        
    # Set study area and name for analysis
    study_area = int(argv[1])
    output_name = str(argv[2])
        
    # Set params
    water_index = 'mndwi'
    index_threshold = 0.00
    baseline_year = '2019'

    ###############################
    # Load DEA CoastLines rasters #
    ###############################
    
    # Load yearly and gapfill data
    yearly_ds, gapfill_ds = load_rasters(output_name, 
                                         study_area, 
                                         water_index)
    # Create output vector folder
    output_dir = f'output_data/{study_area}_{output_name}/vectors'
    os.makedirs(f'{output_dir}/shapefiles', exist_ok=True)

    ####################
    # Load vector data #
    ####################

    # Get bounding box to load data for
    bbox = gpd.GeoSeries(box(*array_bounds(height=yearly_ds.sizes['y'], 
                                           width=yearly_ds.sizes['x'], 
                                           transform=yearly_ds.transform)), 
                         crs=yearly_ds.crs)

    # Rocky shore mask
    smartline_gdf = (gpd.read_file('input_data/Smartline.gdb', 
                                   bbox=bbox)
                     .to_crs(yearly_ds.crs))

    # Tide points
    tide_points_gdf = (gpd.read_file('input_data/tide_points_coastal.geojson', 
                                bbox=bbox)
                       .to_crs(yearly_ds.crs))

    # Study area polygon
    comp_gdf = (gpd.read_file('input_data/50km_albers_grid_clipped.geojson', 
                              bbox=bbox)
                .set_index('id')
                .to_crs(str(yearly_ds.crs)))

    # Mask to study area
    study_area_poly = comp_gdf.loc[study_area]

    # Load climate indices
    climate_df = pd.read_csv('input_data/soi.long.data', 
                             header=None, 
                             delimiter='  ', 
                             skiprows=1, 
                             index_col=0, 
                             skipfooter=10,
                             engine='python').mean(axis=1).to_frame('soi')

    ##############################
    # Extract shoreline contours #
    ##############################

    # Generate waterbody mask
    waterbody_array = waterbody_mask(
        input_data='input_data/SurfaceHydrologyPolygonsRegional.gdb',
        modification_data='input_data/estuary_mask_modifications.geojson',
        bbox=bbox,
        yearly_ds=yearly_ds)

    # Mask dataset to focus on coastal zone only
    masked_ds = contours_preprocess(
        yearly_ds,
        gapfill_ds,
        water_index, 
        index_threshold, 
        waterbody_array, 
        tide_points_gdf,
        output_path=f'output_data/{study_area}_{output_name}')

    # Extract contours
    contours_gdf = subpixel_contours(
        da=masked_ds,
        z_values=index_threshold,
        min_vertices=30,
        dim='year').set_index('year')

    ######################
    # Compute statistics #
    ######################    

    # Extract statistics modelling points along baseline contour
    points_gdf = points_on_line(contours_gdf, baseline_year, distance=30)

    # Clip to remove rocky shoreline points
    points_gdf = rocky_shores_clip(points_gdf, smartline_gdf, buffer=50)
    
    # If any points remain after rocky shoreline clip
    if points_gdf is not None:

        # Calculate annual coastline movements and residual tide heights 
        # for every contour compared to the baseline year
        points_gdf = annual_movements(points_gdf,
                                      contours_gdf,
                                      yearly_ds,                                     
                                      baseline_year,
                                      water_index)

        # Calculate regressions
        points_gdf = calculate_regressions(points_gdf,
                                           contours_gdf,
                                           climate_df)
        
        # Add in retreat/growth helper columns (used for web services)
        points_gdf['retreat'] = points_gdf.rate_time < 0 
        points_gdf['growth'] = points_gdf.rate_time > 0
        
        # Add Shoreline Change Envelope (SCE), Net Shoreline Movement 
        # (NSM) and Max/Min years
        stats_list = ['sce', 'nsm', 'max_year', 'min_year', 'breaks']
        points_gdf[stats_list] = points_gdf.apply(
            lambda x: all_time_stats(x), axis=1)
        
        ################
        # Export stats #
        ################

        if points_gdf is not None:
            
            # Set up scheme to optimise file size
            schema_dict = {key: 'float:8.2' for key in points_gdf.columns
                           if key != 'geometry'}
            schema_dict.update({'sig_time': 'float:8.3',
                                'outl_time': 'str:80',
                                'sig_soi': 'float:8.3',
                                'outl_soi': 'str:80',
                                'retreat': 'bool', 
                                'growth': 'bool',
                                'max_year': 'int:4',
                                'min_year': 'int:4',
                                'breaks': 'str:80'})
            col_schema = schema_dict.items()
            
            # Clip stats to study area extent, remove rocky shores
            stats_path = f'{output_dir}/stats_{study_area}_{output_name}_' \
                         f'{water_index}_{index_threshold:.2f}'
            points_gdf = points_gdf[points_gdf.intersects(study_area_poly['geometry'])]

            # Export to GeoJSON
            points_gdf.to_crs('EPSG:4326').to_file(f'{stats_path}.geojson', 
                                                   driver='GeoJSON')

            # Export as ESRI shapefiles
            stats_path = stats_path.replace('vectors', 'vectors/shapefiles')
            points_gdf.to_file(f'{stats_path}.shp',
                               schema={'properties': col_schema,
                                       'geometry': 'Point'})
    
    ###################
    # Export contours #
    ###################    
    
    # Assign certainty to contours based on underlying masks
    contours_gdf = contour_certainty(
        contours_gdf=contours_gdf, 
        output_path=f'output_data/{study_area}_{output_name}')

    # Clip annual shoreline contours to study area extent
    contour_path = f'{output_dir}/contours_{study_area}_{output_name}_' \
                   f'{water_index}_{index_threshold:.2f}'
    contours_gdf['geometry'] = contours_gdf.intersection(study_area_poly['geometry'])
    contours_gdf.reset_index().to_crs('EPSG:4326').to_file(f'{contour_path}.geojson', 
                                                           driver='GeoJSON')

    # Export stats and contours as ESRI shapefiles
    contour_path = contour_path.replace('vectors', 'vectors/shapefiles')
    contours_gdf.reset_index().to_file(f'{contour_path}.shp')


if __name__ == "__main__":
    main()