""">>> help(buzz.DataSource)"""

# pylint: disable=too-many-lines
import ntpath
import numbers
import sys
import os
import pathlib

from osgeo import osr
import numpy as np

from buzzard._tools import conv, deprecation_pool
from buzzard._footprint import Footprint
from buzzard import _tools
from buzzard._datasource_back import BackDataSource
from buzzard._a_proxy import AProxy
from buzzard._gdal_file_raster import GDALFileRaster, BackGDALFileRaster
from buzzard._gdal_file_vector import GDALFileVector, BackGDALFileVector
from buzzard._gdal_mem_raster import GDALMemRaster
from buzzard._gdal_memory_vector import GDALMemoryVector
from buzzard._datasource_register import DataSourceRegisterMixin
from buzzard._numpy_raster import NumpyRaster
from buzzard._cached_raster_recipe import CachedRasterRecipe

def _concat(fp, array_per_fp, raster):
    """TODO: move to buzz.algo?.concat_arrays
    use is_tiling_bijection?
    """
    band_count = next(iter(array_per_fp.values())).shape[-1]

    arr = np.empty(np.r_[fp.shape, band_count], raster.dtype)
    debug_mask = np.zeros(fp.shape, 'bool')
    for tile, tile_arr in array_per_fp.items():
        assert tuple(tile.shape) == tile_arr.shape[:2]
        slices = tile.slice_in(fp)
        assert np.all(debug_mask[slices] == False), debug_mask[slices].mean()
        debug_mask[slices] = True
        arr[slices] = tile_arr

    assert np.all(debug_mask), debug_mask.mean()
    return arr

class DataSource(DataSourceRegisterMixin):
    """DataSource is a class that stores references to files, it allows quick manipulations
    by assigning a key to each registered file.

    For actions specific to opened files, see Raster, RasterStored and VectorProxy classes

    Parameters
    ----------
    sr_work: None or string (see `Coordinates conversions` below)
    sr_fallback: None or string (see `Coordinates conversions` below)
    sr_forced: None or string (see `Coordinates conversions` below)
    analyse_transformation: bool
        Whether or not to perform a basic analysis on two sr to check their compatibilty
    ogr_layer_lock: one of ('none', 'wait', 'raise')
        Mutex operations when reading or writing vector files
    allow_none_geometry: bool
    allow_interpolation: bool
    max_active: nbr >= 1
        Maximum number of sources active at the same time.
    assert_no_change_on_activation: bool
        When activating a deactivated file, check that the definition did not change
        (see `Sources activation / deactivation` below)

    Example
    -------
    >>> import buzzard as buzz

    Creating DataSource
    >>> ds = buzz.DataSource()

    Opening
    >>> ds.open_vector('roofs', 'path/to/roofs.shp')
    >>> feature_count = len(ds.roofs)

    >>> ds.open_raster('dem', 'path/to/dem.tif')
    >>> data_type = ds.dem.dtype

    Opening with context management
    >>> with ds.open_raster('rgb', 'path/to/rgb.tif').close:
    ...     data_type = ds.rgb.fp
    ...     arr = ds.rgb.get_data()

    >>> with ds.aopen_raster('path/to/rgb.tif').close as rgb:
    ...     data_type = rgb.dtype
    ...     arr = rgb.get_data()

    Creation
    >>> ds.create_vector('targets', 'path/to/targets.geojson', 'point', driver='GeoJSON')
    >>> geometry_type = ds.targets.type

    >>> with ds.acreate_raster('/tmp/cache.tif', ds.dem.fp, 'float32', 1).delete as cache:
    ...     file_footprint = cache.fp
    ...     cache.set_data(dem.get_data())

    On the fly re-projections in buzzard
    ------------------------------------
    A DataSource may perform spatial reference conversions on the fly, like a GIS does. Several
    modes are available, a set of rules define how each mode work. Those conversions concern both
    read operations and write operations, all are performed by OSR.

    Those conversions are only perfomed on vector's data/metadata and raster's Footprints.
    This implies that classic raster warping is not included (yet) in those conversions, only raster
    shifting/scaling/rotation.

    The `z` coordinates of vectors features are also converted, on the other hand elevations are not
    converted in DEM rasters.

    If `analyse_transformation` is set to `True` (default), all coordinates conversions are
    tested against `buzz.env.significant` on file opening to ensure their feasibility or
    raise an exception otherwise. This system is naive and very restrictive, a smarter
    version is planned. Use with caution.

    Terminology:
    `sr`: Spatial reference
    `sr_work`: The sr of all interactions with a DataSource (i.e. Footprints, extents, Polygons...),
        may be missing
    `sr_stored`: The sr that can be found in the metadata of a raster/vector storage, may be None
        or ignored
    `sr_virtual`: The sr considered to be written in the metadata of a raster/vector storage, it is
        often the same as `sr_stored`. When a raster/vector is read, a conversion is performed from
        `sr_virtual` to `sr_work`. When setting vector data, a conversion is performed from
        `sr_work` to `sr_virtual`.
    `sr_forced`: A `sr_virtual` provided by user to ignore all `sr_stored`. This is for exemple
        useful when the `sr` stored in the input files are corrupted.
    `sr_fallback`: A `sr_virtual` provided by user to be used when `sr_stored` is missing. This is
        for exemple useful when an input file can't store a `sr (e.g. DFX).

    DataSource parameters and modes:
    | mode | sr_work | sr_fallback | sr_forced | How is the `sr_virtual` of a raster/vector determined                               |
    |------|---------|-------------|-----------|-------------------------------------------------------------------------------------|
    | 1    | None    | None        | None      | Use `sr_stored`, but no conversion is performed for the lifetime of this DataSource |
    | 2    | string  | None        | None      | Use `sr_stored`, if None raise an exception                                         |
    | 3    | string  | string      | None      | Use `sr_stored`, if None it is considered to be `sr_fallback`                       |
    | 4    | string  | None        | string    | Use `sr_forced`                                                                     |

    - If all opened files are known to be written in a same sr, use `mode 1`. No conversions will
        be performed, this is the safest way to work.
    - If all opened files are known to be written in the same sr but you wish to work in a different
        sr, use `mode 4`. The huge benefit of this mode is that the `driver` specific behaviors
        concerning spatial references have no impacts on the data you manipulate.
    - If you want to manipulate files in different sr, `mode 2` and `mode 3` should be used.
       - Side note: Since the GeoJSON driver cannot store a `sr`, it is impossible to open or
         create a GeoJSON file in `mode 2`.

    A spatial reference parameter may be
    - A path to a file
    - A [textual spatial reference](http://gdal.org/java/org/gdal/osr/SpatialReference.html#SetFromUserInput-java.lang.String-)

    Example
    -------
    mode 1 - No conversions at all
    >>> ds = buzz.DataSource()

    mode 2 - Working with WGS84 coordinates
    >>> ds = buzz.DataSource(
            sr_work='WGS84',
        )

    mode 3 - Working in UTM with DXF files in WGS84 coordinates
    >>> ds = buzz.DataSource(
            sr_work='EPSG:32632',
            sr_fallback='WGS84',
        )

    mode 4 - Working in UTM with unreliable LCC input files
    >>> ds = buzz.DataSource(
            sr_work='EPSG:32632',
            sr_forced='EPSG:27561',
        )

    Sources activation / deactivation
    ---------------------------------
    TODO: Rewrite
    A source may be temporary deactivated, releasing it's internal file descriptor while keeping
    enough informations to reactivate itself later. By setting a `max_activated` different that
    `np.inf` in DataSource constructor, the sources of data are automatically deactivated in a
    lru fashion, and automatically reactivated when necessary.

    Benefits:
    - Open an infinite number of files without worrying about the number of file descriptors allowed
      by the system.
    - Pickle/unpickle a DataSource

    Side notes:
    - A `RasterRecipe` may require the `cloudpickle` library to be pickled
    - All sources open in 'w' mode should be closed before pickling
    - If a source's definition changed between a deactivation and an activation an exception is
      raised (i.e. file changed on the file system)

    """

    def __init__(self, sr_work=None, sr_fallback=None, sr_forced=None,
                 analyse_transformation=True,
                 allow_none_geometry=False,
                 allow_interpolation=False,
                 max_active=np.inf,
                 **kwargs):
        sr_fallback, kwargs = deprecation_pool.streamline_with_kwargs(
            new_name='sr_fallback', old_names={'sr_implicit': '0.4.4'}, context='DataSource.__init__',
            new_name_value=sr_fallback,
            new_name_is_provided=sr_fallback is not None,
            user_kwargs=kwargs,
        )
        sr_forced, kwargs = deprecation_pool.streamline_with_kwargs(
            new_name='sr_forced', old_names={'sr_origin': '0.4.4'}, context='DataSource.__init__',
            new_name_value=sr_forced,
            new_name_is_provided=sr_forced is not None,
            user_kwargs=kwargs,
        )
        max_active, kwargs = deprecation_pool.streamline_with_kwargs(
            new_name='max_active', old_names={'max_activated': '0.5.0'}, context='DataSource.__init__',
            new_name_value=max_active,
            new_name_is_provided=max_active != np.inf,
            user_kwargs=kwargs,
        )
        if kwargs: # pragma: no cover
            raise TypeError("__init__() got an unexpected keyword argument '{}'".format(
                list(kwargs.keys())[0]
            ))

        mode = (sr_work is not None, sr_fallback is not None, sr_forced is not None)
        if mode == (False, False, False):
            pass
        elif mode == (True, False, False):
            sr_work = osr.GetUserInputAsWKT(sr_work)
        elif mode == (True, True, False):
            sr_work = osr.GetUserInputAsWKT(sr_work)
            sr_fallback = osr.GetUserInputAsWKT(sr_fallback)
        elif mode == (True, False, True):
            sr_work = osr.GetUserInputAsWKT(sr_work)
            sr_forced = osr.GetUserInputAsWKT(sr_forced)
        else:
            raise ValueError('Bad combination of `sr_*` parameters') # pragma: no cover

        if max_active < 1: # pragma: no cover
            raise ValueError('`max_active` should be greater than 1')

        allow_interpolation = bool(allow_interpolation)
        allow_none_geometry = bool(allow_none_geometry)
        analyse_transformation = bool(analyse_transformation)

        self._back = BackDataSource(
            wkt_work=sr_work,
            wkt_fallback=sr_fallback,
            wkt_forced=sr_forced,
            analyse_transformation=analyse_transformation,
            allow_none_geometry=allow_none_geometry,
            allow_interpolation=allow_interpolation,
            max_active=max_active,
            ds_id=id(self),
        )
        super(DataSource, self).__init__()

    # Raster entry points *********************************************************************** **
    def open_raster(self, key, path, driver='GTiff', options=(), mode='r'):
        """Open a raster file in this DataSource under `key`. Only metadata are kept in memory.

        Parameters
        ----------
        key: hashable (like a string)
            File identifier within DataSource
        path: string
        driver: string
            gdal driver to use when opening the file
            http://www.gdal.org/formats_list.html
        options: sequence of str
            options for gdal
        mode: one of {'r', 'w'}

        Returns
        -------
        GDALFileRaster

        Example
        -------
        >>> ds.open_raster('ortho', '/path/to/ortho.tif')
        >>> file_proj4 = ds.ortho.proj4_stored

        >>> ds.open_raster('dem', '/path/to/dem.tif', mode='w')
        >>> nodata_value = ds.dem.nodata

        """
        # Parameter checking ***************************************************
        self._validate_key(key)
        path = str(path)
        driver = str(driver)
        options = [str(arg) for arg in options]
        _ = conv.of_of_mode(mode)

        # Construction dispatch ************************************************
        if driver.lower() == 'mem': # pragma: no cover
            raise ValueError("Can't open a MEM raster, user create_raster")
        elif True:
            allocator = lambda: BackGDALFileRaster.open_file(
                path, driver, options, mode
            )
            prox = GDALFileRaster(self, allocator, options, mode)
        else:
            pass

        # DataSource Registering ***********************************************
        self._register([key], prox)
        return prox

    def aopen_raster(self, path, driver='GTiff', options=(), mode='r'):
        """Open a raster file anonymously in this DataSource. Only metadata are kept in memory.

        See DataSource.open_raster

        Example
        ------
        >>> ortho = ds.aopen_raster('/path/to/ortho.tif')
        >>> file_wkt = ds.ortho.wkt_stored

        """
        # Parameter checking ***************************************************
        path = str(path)
        driver = str(driver)
        options = [str(arg) for arg in options]
        _ = conv.of_of_mode(mode)

        # Construction dispatch ************************************************
        if driver.lower() == 'mem': # pragma: no cover
            raise ValueError("Can't open a MEM raster, user acreate_raster")
        elif True:
            allocator = lambda: BackGDALFileRaster.open_file(
                path, driver, options, mode
            )
            prox = GDALFileRaster(self, allocator, options, mode)
        else:
            pass

        # DataSource Registering ***********************************************
        self._register([], prox)
        return prox

    def create_raster(self, key, path, fp, dtype, band_count, band_schema=None,
                      driver='GTiff', options=(), sr=None):
        """Create a raster file and register it under `key` in this DataSource. Only metadata are
        kept in memory.

        Parameters
        ----------
        key: hashable (like a string)
            File identifier within DataSource
        path: string
        fp: Footprint
            Description of the location and size of the raster to create.
        dtype: numpy type (or any alias)
        band_count: integer
            number of bands
        band_schema: dict or None
            Band(s) metadata. (see `Band fields` below)
        driver: string
            gdal driver to use when opening the file
            http://www.gdal.org/formats_list.html
        options: sequence of str
            options for gdal
            http://www.gdal.org/frmt_gtiff.html
        sr: string or None
            Spatial reference of the new file

            if None: don't set a spatial reference
            if string:
                if path: Use same projection as file at `path`
                if textual spatial reference:
                    http://gdal.org/java/org/gdal/osr/SpatialReference.html#SetFromUserInput-java.lang.String-

        Returns
        -------
        one of {GDALFileRaster, GDALMemRaster}
            depending on the `driver` parameter

        Band fields
        -----------
        Fields:
            'nodata': None or number
            'interpretation': None or str
            'offset': None or number
            'scale': None or number
            'mask': None or one of ('')
        Interpretation values:
            undefined, grayindex, paletteindex, redband, greenband, blueband, alphaband, hueband,
            saturationband, lightnessband, cyanband, magentaband, yellowband, blackband
        Mask values:
            all_valid, per_dataset, alpha, nodata

        A field missing or None is kept to default value.
        A field can be passed as:
            a value: All bands are set to this value
            a sequence of length `band_count` of value: All bands will be set to respective state

        Example
        -------
        >>> ds.create_raster('out', 'output.tif', ds.dem.fp, 'float32', 1)
        >>> file_footprint = ds.out.fp

        Caveat
        ------
        When using the GTiff driver, specifying a `mask` or `interpretation` field may lead to unexpected results.

        """
        # Parameter checking ***************************************************
        self._validate_key(key)
        path = str(path)
        if not isinstance(fp, Footprint): # pragma: no cover
            raise TypeError('`fp` should be a Footprint')
        dtype = np.dtype(dtype)
        band_count = int(band_count)
        if band_count <= 0:
            raise ValueError('`band_count` should be >0')
        band_schema = _tools.sanitize_band_schema(band_schema, band_count)
        driver = str(driver)
        options = [str(arg) for arg in options]
        if sr is not None:
            sr = osr.GetUserInputAsWKT(sr)

        if sr is not None:
            fp = self._back.convert_footprint(fp, sr)

        # Construction dispatch ************************************************
        if driver.lower() == 'mem':
            # TODO: Check not concurrent
            prox = GDALMemRaster(
                self, fp, dtype, band_count, band_schema, options, sr
            )
        elif True:
            allocator = lambda: BackGDALFileRaster.create_file(
                path, fp, dtype, band_count, band_schema, driver, options, sr
            )
            prox = GDALFileRaster(self, allocator, options, 'w')
        else:
            pass

        # DataSource Registering ***********************************************
        self._register([key], prox)
        return prox

    def acreate_raster(self, path, fp, dtype, band_count, band_schema=None,
                       driver='GTiff', options=(), sr=None):
        """Create a raster file anonymously in this DataSource. Only metadata are kept in memory.

        See DataSource.create_raster

        Example
        -------
        >>> mask = ds.acreate_raster('mask.tif', ds.dem.fp, bool, 1, options=['SPARSE_OK=YES'])
        >>> open_options = mask.open_options

        >>> band_schema = {
        ...     'nodata': -32767,
        ...     'interpretation': ['blackband', 'cyanband'],
        ... }
        >>> out = ds.acreate_raster('output.tif', ds.dem.fp, 'float32', 2, band_schema)
        >>> band_interpretation = out.band_schema['interpretation']

        """
        # Parameter checking ***************************************************
        path = str(path)
        if not isinstance(fp, Footprint): # pragma: no cover
            raise TypeError('`fp` should be a Footprint')
        dtype = np.dtype(dtype)
        band_count = int(band_count)
        if band_count <= 0:
            raise ValueError('`band_count` should be >0')
        band_schema = _tools.sanitize_band_schema(band_schema, band_count)
        driver = str(driver)
        options = [str(arg) for arg in options]
        if sr is not None:
            sr = osr.GetUserInputAsWKT(sr)

        if sr is not None:
            fp = self._back.convert_footprint(fp, sr)

        # Construction dispatch ************************************************
        if driver.lower() == 'mem':
            # TODO: Check not concurrent
            prox = GDALMemRaster(
                self, fp, dtype, band_count, band_schema, options, sr
            )
        elif True:
            allocator = lambda: BackGDALFileRaster.create_file(
                path, fp, dtype, band_count, band_schema, driver, options, sr
            )
            prox = GDALFileRaster(self, allocator, options, 'w')
        else:
            pass

        # DataSource Registering ***********************************************
        self._register([], prox)
        return prox

    def wrap_numpy_raster(self, key, fp, array, band_schema=None, sr=None, mode='w'):
        """Register a numpy array as a raster under `key` in this DataSource.

        Parameters
        ----------
        key: hashable (like a string)
            File identifier within DataSource
        fp: Footprint of shape (Y, X)
            Description of the location and size of the raster to create.
        array: ndarray of shape (Y, X) or (Y, X, B)
        band_schema: dict or None
            Band(s) metadata. (see `Band fields` below)
        sr: string or None
            Spatial reference of the new file

            if None: don't set a spatial reference
            if string:
                if path: Use same projection as file at `path`
                if textual spatial reference:
                    http://gdal.org/java/org/gdal/osr/SpatialReference.html#SetFromUserInput-java.lang.String-
        mode: one of {'r', 'w'}

        Returns
        -------
        NumpyRaster

        Band fields
        -----------
        Fields:
            'nodata': None or number
            'interpretation': None or str
            'offset': None or number
            'scale': None or number
            'mask': None or one of ('')
        Interpretation values:
            undefined, grayindex, paletteindex, redband, greenband, blueband, alphaband, hueband,
            saturationband, lightnessband, cyanband, magentaband, yellowband, blackband
        Mask values:
            all_valid, per_dataset, alpha, nodata

        A field missing or None is kept to default value.
        A field can be passed as:
            a value: All bands are set to this value
            a sequence of length `band_count` of value: All bands will be set to respective state

        """
        # Parameter checking ***************************************************
        self._validate_key(key)
        if not isinstance(fp, Footprint): # pragma: no cover
            raise TypeError('`fp` should be a Footprint')
        array = np.asarray(array)
        if array.shape[:2] != tuple(fp.shape): # pragma: no cover
            raise ValueError('Incompatible shape between `array` and `fp`')
        if array.ndim not in [2, 3]: # pragma: no cover
            raise ValueError('Array should have 2 or 3 dimensions')
        band_count = 1 if array.ndim == 2 else array.shape[-1]
        band_schema = _tools.sanitize_band_schema(band_schema, band_count)
        if sr is not None:
            sr = osr.GetUserInputAsWKT(sr)
        _ = conv.of_of_mode(mode)

        if sr is not None:
            fp = self._back.convert_footprint(fp, sr)

        # Construction *********************************************************
        prox = NumpyRaster(self, fp, array, band_schema, sr, mode)

        # DataSource Registering ***********************************************
        self._register([key], prox)
        return prox

    def awrap_numpy_raster(self, fp, array, band_schema=None, sr=None, mode='w'):
        """Register a numpy array as a raster anonymously in this DataSource.

        See DataSource.wrap_numpy_raster
        """
        # Parameter checking ***************************************************
        if not isinstance(fp, Footprint): # pragma: no cover
            raise TypeError('`fp` should be a Footprint')
        array = np.asarray(array)
        if array.shape[:2] != tuple(fp.shape): # pragma: no cover
            raise ValueError('Incompatible shape between `array` and `fp`')
        if array.ndim not in [2, 3]: # pragma: no cover
            raise ValueError('Array should have 2 or 3 dimensions')
        band_count = 1 if array.ndim == 2 else array.shape[-1]
        print('//////////////////////////////////////////////////')
        print('awrap_numpy_raster')
        print(band_schema)
        print('//////////////////////////////////////////////////')
        band_schema = _tools.sanitize_band_schema(band_schema, band_count)
        print('//////////////////////////////////////////////////')
        print('awrap_numpy_raster')
        print(band_schema)
        print('//////////////////////////////////////////////////')
        if sr is not None:
            sr = osr.GetUserInputAsWKT(sr)
        _ = conv.of_of_mode(mode)

        if sr is not None:
            fp = self._back.convert_footprint(fp, sr)

        # Construction *********************************************************
        prox = NumpyRaster(self, fp, array, band_schema, sr, mode)

        # DataSource Registering ***********************************************
        self._register([], prox)
        return prox

    # Vector entry points *********************************************************************** **
    def open_vector(self, key, path, layer=None, driver='ESRI Shapefile', options=(), mode='r'):
        """Open a vector file in this DataSource under `key`. Only metadata are kept in memory.

        Parameters
        ----------
        key: hashable (like a string)
            File identifier within DataSource
        path: string
        layer: None or int or string
        driver: string
            ogr driver to use when opening the file
            http://www.gdal.org/ogr_formats.html
        options: sequence of str
            options for ogr
        mode: one of {'r', 'w'}

        Returns
        -------
        GDALFileVector

        Example
        -------
        >>> ds.open_vector('trees', '/path/to.shp')
        >>> feature_count = len(ds.trees)

        >>> ds.open_vector('roofs', '/path/to.json', driver='GeoJSON', mode='w')
        >>> fields_list = ds.roofs.fields

        """
        # Parameter checking ***************************************************
        self._validate_key(key)
        path = str(path)
        if layer is None:
            layer = 0
        elif isinstance(layer, numbers.Integral):
            layer = int(layer)
        else:
            layer = str(layer)
        driver = str(driver)
        options = [str(arg) for arg in options]
        _ = conv.of_of_mode(mode)

        # Construction dispatch ************************************************
        if driver.lower() == 'memory': # pragma: no cover
            raise ValueError("Can't open a MEMORY vector, user create_vector")
        elif True:
            allocator = lambda: BackGDALFileVector.open_file(
                path, layer, driver, options, mode
            )
            prox = GDALFileVector(self, allocator, options, mode)
        else:
            pass

        # DataSource Registering ***********************************************
        self._register([key], prox)
        return prox

    def aopen_vector(self, path, layer=None, driver='ESRI Shapefile', options=(), mode='r'):
        """Open a vector file anonymously in this DataSource. Only metadata are kept in memory.

        See DataSource.open_vector

        Example
        -------
        >>> trees = ds.aopen_vector('/path/to.shp')
        >>> features_bounds = trees.bounds

        """
        path = str(path)
        if layer is None:
            layer = 0
        elif isinstance(layer, numbers.Integral):
            layer = int(layer)
        else:
            layer = str(layer)
        driver = str(driver)
        options = [str(arg) for arg in options]
        _ = conv.of_of_mode(mode)

        # Construction dispatch ************************************************
        if driver.lower() == 'memory': # pragma: no cover
            raise ValueError("Can't open a MEMORY vector, user create_vector")
        elif True:
            allocator = lambda: BackGDALFileVector.open_file(
                path, layer, driver, options, mode
            )
            prox = GDALFileVector(self, allocator, options, mode)
        else:
            pass

        # DataSource Registering ***********************************************
        self._register([], prox)
        return prox

    def create_vector(self, key, path, geometry, fields=(), layer=None,
                      driver='ESRI Shapefile', options=(), sr=None):
        """Create a vector file and register it under `key` in this DataSource. Only metadata are
        kept in memory.

        Parameters
        ----------
        key: hashable (like a string)
            File identifier within DataSource
        path: string
        geometry: string
            name of a wkb geometry type
            http://www.gdal.org/ogr__core_8h.html#a800236a0d460ef66e687b7b65610f12a
            (see example below)
        fields: sequence of dict
            Attributes of fields, one dict per field. (see `Field attributes` below)
        layer: None or string
        driver: string
            ogr driver to use when opening the file
            http://www.gdal.org/ogr_formats.html
        options: sequence of str
            options for ogr
        sr: string or None
            Spatial reference of the new file

            if None: don't set a spatial reference
            if string:
                if path: Use same projection as file at `path`
                if textual spatial reference:
                    http://gdal.org/java/org/gdal/osr/SpatialReference.html#SetFromUserInput-java.lang.String-

        Returns
        -------
        one of {GDALFileVector, GDALMemoryVector} depending on the `driver` parameter

        Field attributes
        ----------------
        Attributes:
            'name': string
            'type': string (see `Field type` below)
            'precision': int
            'width': int
            'nullable': bool
            'default': same as `type`
        An attribute missing or None is kept to default value.

        Field types
        -----------
        Binary        key: 'binary', bytes, np.bytes_, aliases of np.bytes_
        Date          key: 'date'
        DateTime      key: 'datetime', datetime.datetime, np.datetime64, aliases of np.datetime64
        Time          key: 'time'

        Integer       key: 'integer' np.int32, aliases of np.int32
        Integer64     key: 'integer64', int, np.int64, aliases of np.int64
        Real          key: 'real', float, np.float64, aliases of np.float64
        String        key: 'string', str, np.str_, aliases of np.str_

        Integer64List key: 'integer64list', 'int list'
        IntegerList   key: 'integerlist'
        RealList      key: 'reallist', 'float list'
        StringList    key: 'stringlist', 'str list'

        Example
        -------
        >>> ds.create_vector('lines', '/path/to.shp', 'linestring')
        >>> geometry_type = ds.lines.type

        >>> fields = [
            {'name': 'name', 'type': str},
            {'name': 'count', 'type': 'int32'},
            {'name': 'area', 'type': np.float64, 'width': 5, precision: 18},
            {'name': 'when', 'type': np.datetime64},
        ]
        >>> ds.create_vector('zones', '/path/to.shp', 'polygon', fields)
        >>> field0_type = ds.zones.fields[0]['type']

        """
        # Parameter checking ***************************************************
        self._validate_key(key)
        path = str(path)
        geometry = conv.str_of_wkbgeom(conv.wkbgeom_of_str(geometry))
        fields = _tools.normalize_fields_defn(fields)
        if layer is None:
            layer = '.'.join(ntpath.basename(path).split('.')[:-1])
        else:
            layer = str(layer)
        driver = str(driver)
        options = [str(arg) for arg in options]
        if sr is not None:
            sr = osr.GetUserInputAsWKT(sr)

        # Construction dispatch ************************************************
        if driver.lower() == 'memory':
            # TODO: Check not concurrent
            allocator = lambda: BackGDALFileVector.create_file(
                '', geometry, fields, layer, 'Memory', options, sr
            )
            prox = GDALMemoryVector(self, allocator, options)
        elif True:
            allocator = lambda: BackGDALFileVector.create_file(
                path, geometry, fields, layer, driver, options, sr
            )
            prox = GDALFileVector(self, allocator, options, 'w')
        else:
            pass

        # DataSource Registering ***********************************************
        self._register([key], prox)
        return prox

    def acreate_vector(self, path, geometry, fields=(), layer=None,
                       driver='ESRI Shapefile', options=(), sr=None):
        """Create a vector file anonymously in this DataSource. Only metadata are kept in memory.

        See DataSource.create_vector

        Example
        -------
        >>> lines = ds.acreate_vector('/path/to.shp', 'linestring')
        >>> file_proj4 = lines.proj4_stored

        """
        # Parameter checking ***************************************************
        path = str(path)
        geometry = conv.str_of_wkbgeom(conv.wkbgeom_of_str(geometry))
        fields = _tools.normalize_fields_defn(fields)
        if layer is None:
            layer = '.'.join(ntpath.basename(path).split('.')[:-1])
        else:
            layer = str(layer)
        driver = str(driver)
        options = [str(arg) for arg in options]
        if sr is not None:
            sr = osr.GetUserInputAsWKT(sr)

        # Construction dispatch ************************************************
        if driver.lower() == 'memory':
            # TODO: Check not concurrent
            allocator = lambda: BackGDALFileVector.create_file(
                '', geometry, fields, layer, 'Memory', options, sr
            )
            prox = GDALMemoryVector(self, allocator, options)
        elif True:
            allocator = lambda: BackGDALFileVector.create_file(
                path, geometry, fields, layer, driver, options, sr
            )
            prox = GDALFileVector(self, allocator, options, 'w')
        else:
            pass

        # DataSource Registering ***********************************************
        self._register([], prox)
        return prox

    def create_raster_recipe(self, key, fp, dtype, band_count, band_schema=None, sr=None,
                             compute_array=None, merge_arrays=_concat,
                             queue_data_per_primitive={}, convert_footprint_per_primitive=None,
                             computation_pool='cpu', merge_pool='cpu', resample_pool='cpu',
                             max_computation_size=None, max_resampling_size=None,
                             remap_in_primitives=False):
        """Create a raster recipe and register it under `key` in this DataSource.

        TODO: Fill

        Parameters
        ----------
        key: hashable (like a string)
            File identifier within DataSource
        fp: Footprint
            Description of the location and size of the raster to create.
        dtype: numpy type (or any alias)
        band_count: integer
            number of bands
        band_schema: dict or None
            Band(s) metadata. (see `Band fields` below)
        sr: string or None
            Spatial reference of the new file

            if None: don't set a spatial reference
            if string:
                if path: Use same projection as file at `path`
                if textual spatial reference:
                    http://gdal.org/java/org/gdal/osr/SpatialReference.html#SetFromUserInput-java.lang.String-

        compute_array: function with prototype f(Footprint, list(Footprint), list(np.ndarray), RasterRecipe) -> np.ndarray
            from a footprint and a set of data (footprint + ndarray) returns a ndarray correspondig to footprint
        merge_arrays: function with prototype f(Footprint, list(Footprint), list(np.ndarray)) -> np.ndarray
            from a footprint and a set of data (footprint + ndarray) returns a merged ndarray correspondig to footprint
        queue_data_per_primitive: dict of callable
            should be the bound `queue_data` method of another ScheduledRaster in the same DataSource.
            can also be a functools.partial instance to that method
            (see queue_data below) TODO
        convert_footprint_per_primitive: function f(Footprint) -> dict
            dict is key (same as above) and value: Footprint
        computation_pool: str or multiprocessing.pool.ThreadPool or multiprocessing.pool.Pool or None
            if None, operation done on scheduler
        merge_pool: str or multiprocessing.pool.ThreadPool or multiprocessing.pool.Pool or None
            if None, operation done on scheduler
        resample_pool: str or multiprocessing.pool.ThreadPool or multiprocessing.pool.Pool or None
            if None, operation done on scheduler
        max_computation_size: None or int or (int, int)
        max_resampling_size: None or int or (int, int)
        remap_in_primitives: bool
            if True: defer the remap operations in the primitives
            if False: does the remap when producing

        Returns
        -------
        RasterRecipe with get_data, queue_data and iter_data entry points

        Band fields
        -----------
        Fields:
            'nodata': None or number
            'interpretation': None or str
            'offset': None or number
            'scale': None or number
            'mask': None or one of ('')
        Interpretation values:
            undefined, grayindex, paletteindex, redband, greenband, blueband, alphaband, hueband,
            saturationband, lightnessband, cyanband, magentaband, yellowband, blackband
        Mask values:
            all_valid, per_dataset, alpha, nodata

        A field missing or None is kept to default value.
        A field can be passed as:
            a value: All bands are set to this value
            a sequence of length `band_count` of value: All bands will be set to respective state

        """
        pass

    def create_cached_raster_recipe(self, key, fp, dtype, band_count, band_schema=None, sr=None,
                                    # TODO: reorder parameters
                                    # TODO: overwrite
                                    compute_array=None, merge_arrays=_concat,
                                    cache_dir=None,
                                    queue_data_per_primitive={}, convert_footprint_per_primitive=None,
                                    computation_pool='cpu', merge_pool='cpu', io_pool='io', resample_pool='cpu',
                                    cache_tiles=(512, 512), computation_tiles=None,
                                    max_resampling_size=None,
                                    debug_observers=()):
        """Create a raster cached recipe and register it under `key` in this DataSource.

        TODO: Fill

        Parameters
        ----------
        key: hashable (like a string)
            File identifier within DataSource
        fp: Footprint
            Description of the location and size of the raster to create.
        dtype: numpy type (or any alias)
        band_count: integer
            number of bands
        band_schema: dict or None
            Band(s) metadata. (see `Band fields` below)
        sr: string or None
            Spatial reference of the new file
            if None: don't set a spatial reference
            if string:
                if path: Use same projection as file at `path`
                if textual spatial reference:
                    http://gdal.org/java/org/gdal/osr/SpatialReference.html#SetFromUserInput-java.lang.String-
        compute_array: function with prototype f(Footprint, list(Footprint), list(np.ndarray), RasterRecipe) -> np.ndarray
            from a footprint and a set of data (footprint + ndarray) returns a ndarray correspondig to footprint
        merge_arrays: function with prototype f(Footprint, list(Footprint), list(np.ndarray)) -> np.ndarray
            from a footprint and a set of data (footprint + ndarray) returns a merged ndarray correspondig to footprint
        queue_data_per_primitive: dict of callable
            should be the bound `queue_data` method of another ScheduledRaster in the same DataSource
            can also be a functools.partial instance to that method
            (see queue_data below) TODO
        convert_footprint_per_primitive: function f(Footprint) -> dict
            dict is key (same as above) and value: Footprint
        computation_pool: str or multiprocessing.pool.ThreadPool or multiprocessing.pool.Pool or None
            if None, operation done on scheduler
        merge_pool: str or multiprocessing.pool.ThreadPool or multiprocessing.pool.Pool or None
            if None, operation done on scheduler
        resample_pool: str or multiprocessing.pool.ThreadPool or multiprocessing.pool.Pool or None
            if None, operation done on scheduler
        io_pool: str or multiprocessing.pool.ThreadPool or multiprocessing.pool.Pool or None
            if None, operation done on scheduler
        cache_dir: str
        cache_tiles:
            if (int, int): Construct the tiling by calling Footprint.tile with this tile size
        computation_tiles: None or np.ndarray of Footprint or shape (TY, TX) or (int, int)
            if None: Use the same tiling as cache_tiles
            if (int, int): Construct the tiling by calling Footprint.tile with this tile size
        max_resampling_size: None or int or (int, int)

        Returns
        -------
        RasterCachedRecipe with get_data, queue_data and iter_data entry points
        """
        # Parameter checking ***************************************************
        # Classic RasterProxy parameters *******************
        self._validate_key(key)
        if not isinstance(fp, Footprint): # pragma: no cover
            raise TypeError('`fp` should be a Footprint')
        dtype = np.dtype(dtype)
        band_count = int(band_count)
        if band_count <= 0:
            raise ValueError('`band_count` should be >0')
        band_schema = _tools.sanitize_band_schema(band_schema, band_count)
        if sr is not None:
            sr = osr.GetUserInputAsWKT(sr)
        if sr is not None:
            fp = self._back.convert_footprint(fp, sr)

        # Callables ****************************************
        if not callable(compute_array):
            raise TypeError('`compute_array` should be callable')
        if not callable(merge_arrays):
            raise TypeError('`merge_arrays` should be callable')

        # Primitives ***************************************
        if convert_footprint_per_primitive is None:
            convert_footprint_per_primitive = {
                name: (lambda fp: fp)
                for name in queue_data_per_primitive.keys()
            }

        if queue_data_per_primitive.keys() != convert_footprint_per_primitive.keys():
            err = 'There should be the same keys in `queue_data_per_primitive` and '
            err += '`convert_footprint_per_primitive`.'
            if queue_data_per_primitive.keys() - convert_footprint_per_primitive.keys():
                err += '\n{} are missing from `convert_footprint_per_primitive`.'.format(
                    queue_data_per_primitive.keys() - convert_footprint_per_primitive.keys()
                )
            if convert_footprint_per_primitive.keys() - queue_data_per_primitive.keys():
                err += '\n{} are missing from `queue_data_per_primitive`.'.format(
                    convert_footprint_per_primitive.keys() - queue_data_per_primitive.keys()
                )
            raise ValueError(err)

        primitives_back = {}
        primitives_kwargs = {}
        for name, met in queue_data_per_primitive.items():
            primitives_back[name], primitives_kwargs[name] = _tools.shatter_queue_data_method(met, name)

        for name, func in convert_footprint_per_primitive.items():
            if not callable(func):
                raise TypeError('convert_footprint_per_primitive[{}] should be callable'.format(
                    name
                ))

        # Pools ********************************************
        computation_pool = self._back.normalize_pool_parameter(
            computation_pool, 'computation_pool'
        )
        merge_pool = self._back.normalize_pool_parameter(
            merge_pool, 'merge_pool'
        )
        io_pool = self._back.normalize_pool_parameter(
            io_pool, 'io_pool'
        )
        resample_pool = self._back.normalize_pool_parameter(
            resample_pool, 'resample_pool'
        )

        # Tilings ******************************************
        if isinstance(cache_tiles, np.ndarray) and cache_tiles.dtype == np.object:
            if not _tools.is_tiling_bijection_of(cache_tiles, fp):
                raise ValueError("`cache_tiles` should be a tiling of raster's Footprint, " +\
                                "without overlap, with `boundary_effect='shrink'`"
                )
        else:
            # Defer the parameter checking to fp.tile
            cache_tiles = fp.tile(cache_tiles, 0, 0, boundary_effect='shrink')

        if computation_tiles is None:
            computation_tiles = cache_tiles
        elif isinstance(computation_tiles, np.ndarray) and computation_tiles.dtype == np.object:
            # TODO: computation_tiles should be able to go out of bounds!!
            if not _tools.is_tiling_surjection_of(computation_tiles, fp):
                raise ValueError("`computation_tiles` should be a tiling of raster's Footprint, " +\
                                "with `boundary_effect='shrink'`"
               )
        else:
            # Defer the parameter checking to fp.tile
            computation_tiles = fp.tile(computation_tiles, 0, 0, boundary_effect='shrink')

        # Misc *********************************************
        if max_resampling_size is not None:
            max_resampling_size = int(max_resampling_size)
            if max_resampling_size <= 1:
                raise ValueError('`max_resampling_size` should be >0')

        if not isinstance(cache_dir, (str, pathlib.Path)):
            raise TypeError('cache_dir should be a string')
        cache_dir = str(cache_dir)
        os.makedirs(cache_dir, exist_ok=True)

        # Construction *********************************************************
        prox = CachedRasterRecipe(
            self,
            fp, dtype, band_count, band_schema, sr,
            compute_array, merge_arrays,
            cache_dir, primitives_back, primitives_kwargs, convert_footprint_per_primitive,
            computation_pool, merge_pool, io_pool, resample_pool,
            cache_tiles,computation_tiles,
            max_resampling_size,
            debug_observers,
        )

        # DataSource Registering ***********************************************
        self._register([key], prox)
        return prox

    # Proxy getters ********************************************************* **
    def __getitem__(self, key):
        """Retrieve a proxy from its key"""
        return self._proxy_of_key[key]

    def __contains__(self, item):
        """Is key or proxy registered in DataSource"""
        if isinstance(item, AProxy):
            return item in self._keys_of_proxy
        return item in self._proxy_of_key

    def __len__(self):
        """Retrieve proxy count registered in this DataSource"""
        return len(self._keys_of_proxy)

    def __del__(self): # TODO: `ds.close()`
        self._back.stop_scheduler()

    def items(self):
        """Generate the pair of (keys_of_proxy, proxy) for all proxies"""
        for proxy, keys in self._keys_of_proxy.items():
            yield list(keys), proxy

    # Spatial reference getters ********************************************* **
    @property
    def proj4(self):
        """DataSource's work spatial reference in WKT proj4.
        Returns None if none set.
        """
        if self._back.wkt_work is None:
            return None
        return osr.SpatialReference(self._back.wkt_work).ExportToProj4()

    @property
    def wkt(self):
        """DataSource's work spatial reference in WKT format.
        Returns None if none set.
        """
        return self._back.wkt_work

    # Activation mechanisms ********************************************************************* **
    @property
    def active_count(self):
        """Count how many driver objects are currently active"""
        return self._back.active_count()

    def activate_all(self):
        """Activate all deactivable proxies.
        May raise an exception if the number of sources is greater than `max_activated`
        """
        if self._back.max_active < len(self._keys_of_proxy):
            raise RuntimeError("Can't activate all sources at the same time: {} sources and max_activated is {}".format(
                len(self._keys_of_proxy), self._back.max_active,
            ))
        for prox in self._keys_of_proxy.keys():
            if not prox.active:
                prox.activate()

    def deactivate_all(self):
        """Deactivate all deactivable proxies. Useful to flush all files to disk"""
        for prox in self._keys_of_proxy.keys():
            if prox.active:
                prox.deactivate()


    # Deprecation ******************************************************************************* **
    open_araster = deprecation_pool.wrap_method(
        aopen_raster,
        '0.4.4'
    )
    create_araster = deprecation_pool.wrap_method(
        acreate_raster,
        '0.4.4'
    )
    open_avector = deprecation_pool.wrap_method(
        aopen_vector,
        '0.4.4'
    )
    create_avector = deprecation_pool.wrap_method(
        acreate_vector,
        '0.4.4'
    )

    # The end *********************************************************************************** **
    # ******************************************************************************************* **

if sys.version_info < (3, 6):
    # https://www.python.org/dev/peps/pep-0487/
    for k, v in DataSource.__dict__.items():
        if hasattr(v, '__set_name__'):
            v.__set_name__(DataSource, k)

def open_raster(*args, **kwargs):
    """Shortcut for `DataSource().aopen_raster`"""
    return DataSource().aopen_raster(*args, **kwargs)

def open_vector(*args, **kwargs):
    """Shortcut for `DataSource().aopen_vector`"""
    return DataSource().aopen_vector(*args, **kwargs)

def create_raster(*args, **kwargs):
    """Shortcut for `DataSource().acreate_raster`"""
    return DataSource().acreate_raster(*args, **kwargs)

def create_vector(*args, **kwargs):
    """Shortcut for `DataSource().acreate_vector`"""
    return DataSource().acreate_vector(*args, **kwargs)

def wrap_numpy_raster(*args, **kwargs):
    """Shortcut for `DataSource().awrap_numpy_raster`"""
    return DataSource().awrap_numpy_raster(*args, **kwargs)
