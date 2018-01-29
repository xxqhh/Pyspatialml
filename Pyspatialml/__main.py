import numpy as np
import rasterio
import os
from warnings import warn
import shapely
import geopandas
from rasterio import features
import tempfile

def printProgressBar(iteration, total, prefix = '', suffix = '', decimals = 1,
                     length = 100, fill = '█'):
    """Call in a loop to create terminal progress bar
    https://stackoverflow.com/questions/3173320/text-progress-bar-in-the-console/27871113

    Parameters
    ----------
    iteration: int
        Current iteration
    total: int
        Total iterations
    prefix: str, optional
        prefix string (Str)
    suffix: str, optional
        suffix
    decimals: int, optional
        Positive number of decimals in percent complete
    length: int, optional
        Character length of bar
    fill: str, optional
        bar fill character
    """
    percent = ("{0:." + str(decimals) + "f}").format(100 * (iteration / float(total)))
    filledLength = int(length * iteration // total)
    bar = fill * filledLength + '-' * (length - filledLength)
    print('\r%s |%s| %s%% %s' % (prefix, bar, percent, suffix), end = '\r')
    # Print New Line on Complete
    if iteration == total:
        print()


class RasterStack(object):
    """Access a group of aligned GDAL-supported raster images as a single
    dataset

    Attibutes
    ---------
    names : list (str)
        List of names of rasters in the RasterStack
        Defined by default by filename (sans file extension)
    count : int
        Number of raster layers
    shape : tuple (int)
        Shape of raster data in (n_rows, n_cols)
    height : int
        Height (number of rows) in the raster data
    width : int
        Width (number of columns) in the raster data
    meta : Dict
        The basic metadata of the dataset
    affine : Affine class
        Affine transformation matrix
    bounds : rasterio.coords.BoundingBox
        Bounding box named tuple, defining extent in cartesian coordinates
        BoundingBox(left, bottom, right, top)
    """

    def __init__(self, rasters):
        """Create a RasterStack object

        Parameters
        ----------
        rasters : list (str)
            List of file paths to GDAL-supported rasters to create a
            RasterStack object. The rasters can contain single or multiple
            bands, but they need to be aligned in terms of their width, height,
            and transform.
        """
        self.__check_alignment(rasters)
        # open bands
        self.names = []
        self.count = 0

        for r in rasters:
            bandname = os.path.basename(r)
            bandname = os.path.splitext(bandname)[0]
            self.names.append(bandname)
            src = rasterio.open(r)
            self.count += src.count
            setattr(self, bandname, src)

        self.shape = (self.count, src.shape[0], src.shape[1])
        self.height = src.shape[0]
        self.width = src.shape[1]
        self.meta = src.meta.copy()
        self.meta['count'] = self.count
        self.affine = self.meta['affine']
        self.bounds = src.bounds

    def __check_alignment(self, rasters):
        """Check that a list of rasters are aligned with the same pixel
        dimensions and geotransforms

        Parameters
        ----------
        rasters: list (str)
            List of file paths to the rasters
        """

        src_meta = []
        for r in rasters:
            src = rasterio.open(r)
            src_meta.append(src.meta.copy())
            src.close()

        if not all(i['crs'] == src_meta[0]['crs'] for i in src_meta):
            warn('crs of all rasters does not match possible unintended consequences')

        if not all([i['height'] == src_meta[0]['height'] or
                    i['width'] == src_meta[0]['width'] or
                    i['transform'] == src_meta[0]['transform']
                    for i in src_meta]):
            print('Predictor rasters do not all have the same dimensions or',
                  'transform')
            print('Use the .utils_align_rasters function')

    def read(self, window=None):
        """Read data from RasterStack as a 3D numpy array

        Parameters
        ----------
        window : tuple
            A pair of tuples ((row_start, row_stop), (col_start, col_stop))
            to read a window of raster data as a 3D numpy array

        Returns
        -------
        data : 3D array-like
            3D masked numpy array containing data from RasterStack rasters
        """
        # create numpy array for stack
        if window:
            row_start = window[0][0]
            row_stop = window[0][1]
            col_start = window[1][0]
            col_stop = window[1][1]
            width = abs(col_stop-col_start)
            height = abs(row_stop-row_start)
            shape = (self.count, height, width)
        else:
            shape = self.shape

        data = np.ma.zeros(shape)

        # loop and read data into numpy
        i = 0
        for raster in self.names:
            src = getattr(self, raster)
            data[i:i+src.count, :, :] = src.read(masked=True, window=window)
            i += src.count

        return data

    def predict(self, estimator, output, predict_type='raw', index=None,
                rowchunk=25):
        """Prediction on list of GDAL rasters using a fitted scikit learn model

        Parameters
        ----------
        estimator : estimator object implementing 'fit'
            The object to use to fit the data.
        output : str
            Path to a GeoTiff raster for the classification results
        predict_type : str, optional (default='raw')
            'raw' for classification/regression
            'prob' for probabilities,
            'all' for both classification and probabilities
        index : List, int, optional
            List of class indices to export
        rowchunk : int, optional (default=25)
            Number of raster rows to process at one time
        """
        if isinstance(index, int):
            index = [index]

        # processing region dimensions
        rows, cols = self.height, self.width
        n_features = self.count

        # generator for row increments, tuple(startrow, endrow)
        windows=((row, row+rowchunk) if row+rowchunk <= rows else
                 (row, rows) for row in range(0, rows, rowchunk))

        # Loop through rasters block-by-block
        for start, end, in windows:
            printProgressBar(start, rows, length=50)

            # get raster data for window (bands, blocksize, columns)
            img_np = self.read(window=((start, end), (0, cols)))

            # reshape each image block matrix into a 2D matrix
            # first reorder into rows,cols,bands(transpose)
            # then resample into 2D array (rows=sample_n, cols=band_values)
            n_samples = (end-start) * cols
            flat_pixels = img_np.transpose(1, 2, 0).reshape(
                (n_samples, n_features))

            # create mask for NaN values and replace with number
            flat_pixels_mask = flat_pixels.mask.copy()

            # perform the prediction for classification
            if predict_type == 'raw' or predict_type == 'all':
                result_cla = estimator.predict(flat_pixels)

                # replace mask and fill masked values with nodata value
                result_cla = np.ma.masked_array(
                    result_cla, mask=flat_pixels_mask.any(axis=1))
                result_cla = np.ma.filled(result_cla, fill_value=-99999)

                # reshape the prediction from a 1D matrix/list
                # back into the original format
                result_cla = result_cla.reshape((end-start, cols))

                # write rowchunks to rasterio raster
                if start == 0:
                    clf_output = rasterio.open(
                        output, mode='w', driver='GTiff', height=rows,
                        width=cols, count=1, dtype='float32',
                        crs=self.meta['crs'], transform=self.meta['transform'],
                        nodata=-99999)

                clf_output.write(
                    result_cla.astype('float32'),
                    window=((start, end), (0, cols)), indexes=1)

            # perform the prediction for probabilities
            if predict_type == 'prob' or predict_type == 'all':
                result_proba = estimator.predict_proba(flat_pixels)
                if start == 0:
                    if index is None:
                        index = range(result_proba.shape[1])
                        n_bands = len(index)
                    else:
                        n_bands = len(np.unique(index))

                    fname, ext = os.path.basename(output).split(os.path.extsep)
                    output_proba = os.path.join(
                        os.path.dirname(output),
                        fname + '_proba' +
                        os.path.extsep + ext)
                    proba_output = rasterio.open(
                        output_proba, mode='w', driver='GTiff', height=rows,
                        width=cols, count=n_bands, dtype='float32',
                        crs=self.meta['crs'], transform=self.meta['transform'],
                        nodata=-99999)

                for i_class, label in enumerate(index):
                    result_proba_i = result_proba[:, label]

                    # replace mask and fill masked values with nodata value
                    result_proba_i = np.ma.masked_array(
                        result_proba_i, mask=flat_pixels_mask.any(axis=1),
                        fill_value=np.nan)

                    result_proba_i = np.ma.filled(
                        result_proba_i, fill_value=-99999)

                    # reshape the prediction from a 1D matrix/list back into
                    # the original format
                    result_proba_i = result_proba_i.reshape(
                        ((end-start), cols))

                    # write rowchunks to rasterio raster
                    proba_output.write(
                        result_proba_i.astype('float32'),
                        window=((start, end), (0, cols)),
                        indexes=1+i_class)

        if predict_type == 'raw' or predict_type == 'all':
            clf_output.close()
        if predict_type == 'prob' or predict_type == 'all':
            proba_output.close()


def extract(x, y):
    if isinstance(y, geopandas.geodataframe.GeoDataFrame):
        if isinstance(y.geometry.all(), shapely.geometry.point.Point):
            data = __extract_points(x, y)

    return data


def __extract_points(x, y):
    """Samples a list of GDAL rasters using a point data set

    Parameters
    ----------
    x : RasterStack object
        RasterStack of GDAL-supported rasters
    y : Geopandas GeoDataFrame
        GeoSeries is assumed to entirely consist of point-type feature classes

    Returns
    -------
    gdf : Geopandas GeoDataFrame
        GeoDataFrame containing extract raster values at the point locations
    """

    from rasterio.sample import sample_gen

    # get coordinates and label for each point in points_gdf
    coordinates = np.array(y.bounds.iloc[:, :2])

    # loop through each raster
    for r in x.names:
        raster = getattr(x, r)
        nodata = raster.nodata
        data = np.array([i for i in sample_gen(raster, coordinates)],
                        dtype='float32')
        data[data == nodata] = np.nan
        if data.shape[1] == 1:
            y[r] = data
        else:
            for i in range(1, data.shape[1]+1):
                y[r+'_'+str(i)] = data[:, i-1]

    return (y)


def __extract_polygons(gdf, rasters, field=None, na_rm=True, lowmem=False):
    """Samples a list of GDAL rasters, collecting all pixel values that fall
    within the areas of each polygon feature in a Geopandas GeoDataFrame

    Parameters
    ----------
    gdf : Geopandas DataFrame
        GeoDataFrame where the GeoSeries is assumed to consist entirely of
        Polygon feature class types
    rasters : list, str
        Paths to GDAL supported rasters
    field : str
        Field name of attribute to be used the label the extracted data

    Returns
    -------
    X: array-like
        Numpy array of extracted raster values, typically 2d array-like
    y: 1d array-like
        Numpy array of labels
    coordinates: 2d array-like
        Numpy array of spatial coordinates (x,y) that correspond to each pixel
        that was sampled in the rasters list
    """

    template = rasterio.open(rasters[0], mode='r')
    response_np = np.zeros((template.height, template.width))
    response_np[:] = -99999

    # this is where we create a generator of geom to use in rasterizing
    if field is None:
        shapes = (geom for geom in gdf.geometry)

    if field is not None:
        shapes = ((geom, value) for geom, value in zip(
                  gdf.geometry, gdf[field]))

    features.rasterize(
        shapes=shapes, fill=-99999, out=response_np,
        transform=template.transform, default_value=1)

    response_np = np.ma.masked_where(response_np == -99999, response_np)

    X, y, y_indexes = __extract_pixels(
            response_np, rasters, field=None, na_rm=True, lowmem=False)

    return(X, y, y_indexes)


def __extract_pixels(response_np, rasters, na_rm=True, lowmem=False):
    """Samples a list of GDAL rasters using a labelled numpy array

    Parameters
    ----------
    response_np : masked 2d numpy array
        Masked 2D numpy array containing the pixels of a raster that are
        labelled
    rasters : list, str
        List of paths to GDAL supported rasters
    na_rm : bool, optional (default=True)
        Optionally remove any samples that contain nodata values
    lowmem : bool, optional (default=False)
        Use low memory version using numpy memmap

    Returns
    -------
    X : array-like
        Numpy array of extracted raster values, typically 2d
    y: 1d array like
        Numpy array of labelled sampled
    y_indexes: 2d array-like
        Numpy array of row and column indexes of training pixels
    """

    # determine number of predictors
    n_features = len(rasters)

    # returns indices of labelled values
    is_train = np.nonzero(~response_np.mask)

    # get the labelled values
    training_labels = response_np.data[is_train]
    n_labels = np.array(is_train).shape[1]

    # Create a masked array with the dimensions of the number of columns
    training_data = np.ma.zeros((n_labels, n_features))
    training_data[:] = np.nan

    # Loop through each raster and extract values at training locations
    if lowmem is True:
        template = rasterio.open(rasters[0])
        feature = np.memmap(tempfile.NamedTemporaryFile(),
                            dtype='float32', mode='w+',
                            shape=(template.height, template.width))

    for band, rasterfile in enumerate(rasters):

        # open rasterio
        rio_rast = rasterio.open(rasterfile)

        if lowmem is False:
            feature = rio_rast.read(1, masked=True)
            training_data[0:n_labels, band] = feature[is_train]
        else:
            feature[:] = rio_rast.read(1, masked=True)[:]
            training_data[0:n_labels, band] = \
                np.ma.masked_where(
                    feature[is_train] == rio_rast.nodata,
                    feature[is_train])

    # convert indexes of training pixels from tuple to n*2 np array
    is_train = np.array(is_train).T

    # Remove nan rows from training data
    training_data = training_data.filled(np.nan)

    if na_rm is True:
        X = training_data[~np.isnan(training_data).any(axis=1)]
        y = training_labels[~np.isnan(training_data).any(axis=1)]
        y_indexes = is_train[~np.isnan(training_data).any(axis=1)]
    else:
        X = training_data
        y = training_labels
        y_indexes = is_train

    coordinates = np.array(rasterio.transform.xy(
        transform=rio_rast.transform, rows=y_indexes[:, 0],
        cols=y_indexes[:, 1], offset='center')).T

    return (X, y, coordinates)


def SampleStack(size, rasters, random_state=None):
    """Generates a random sample of according to size, and samples the pixel
    values within the list of rasters

    Parameters
    ----------
    size : int
        Number of random samples to obtain
    rasters : list, str
        List of paths to GDAL supported rasters
    random_state : int
        integer to use within random.seed

    Returns
    -------
    valid_samples: array-like
        Numpy array of extracted raster values, typically 2d
    valid_coordinates: 2d array-like
        2D numpy array of xy coordinates of extracted values
    """

    # set the seed
    np.random.seed(seed=random_state)

    # determine number of GDAL rasters to sample
    n_features = len(rasters)

    # open rasters
    dataset = [0] * n_features
    for n in range(n_features):
        dataset[n] = rasterio.open(rasters[n], mode='r')

    # create np array to store randomly sampled data
    # we are starting with zero initial rows because data will be appended,
    # and number of columns are equal to n_features
    valid_samples = np.zeros((0, n_features))
    valid_coordinates = np.zeros((0, 2))

    # loop until target number of samples is satified
    satisfied = False

    n = size
    while satisfied is False:

        # generate random row and column indices
        Xsample = np.random.choice(range(0, dataset[0].shape[1]), n)
        Ysample = np.random.choice(range(0, dataset[0].shape[0]), n)

        # create 2d numpy array with sample locations set to 1
        sample_raster = np.empty((dataset[0].shape[0], dataset[0].shape[1]))
        sample_raster[:] = np.nan
        sample_raster[Ysample, Xsample] = 1

        # get indices of sample locations
        is_train = np.nonzero(np.isnan(sample_raster) == False)

        # create ma array with rows=1 and cols=n_features
        samples = np.ma.masked_all((len(is_train[0]), n_features))

        # loop through each raster and sample
        for r in range(n_features):
            feature = dataset[r].read(1, masked=True)
            samples[0:n, r] = feature[is_train]

        # append only non-masked data to each row of X_random
        samples = samples.filled(np.nan)

        valid_samples = np.append(
                valid_samples, samples[~np.isnan(samples).any(axis=1)], axis=0)

        is_train = np.array(is_train).T
        valid_coordinates = np.append(valid_coordinates,
                                      is_train[~np.isnan(samples).any(axis=1)],
                                      axis=0)

        # check to see if target_nsamples has been reached
        if len(valid_samples) >= size:
            satisfied = True
        else:
            n = size - len(valid_samples)

    return (valid_samples, valid_coordinates)
