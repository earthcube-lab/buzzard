import uuid
import numbers
import contextlib

from osgeo import gdal

from buzzard._a_pooled_emissary_vector import *
from buzzard._a_gdal_vector import *
from buzzard._tools import conv

class GDALFileVector(APooledEmissaryVector):
    """Proxy for file vector GDAL datasets"""

    def __init__(self, ds, allocator, open_options, mode, layer):
        back = BackGDALFileVector(
            ds._back, allocator, open_options, mode, layer,
        )
        super(GDALFileVector, self).__init__(ds=ds, back=back)

class BackGDALFileVector(ABackPooledEmissaryVector, ABackGDALVector):
    """Implementation of GDALFileVector"""

    def __init__(self, back_ds, allocator, open_options, mode, layer):
        uid = uuid.uuid4()

        with back_ds.acquire_driver_object(uid, allocator) as gdal_objs:
            gdal_ds, lyr = gdal_objs
            rect = None
            if lyr is not None:
                rect = lyr.GetExtent()
            path = gdal_ds.GetDescription()
            driver = gdal_ds.GetDriver().ShortName
            wkt_stored = lyr.GetSpatialRef().ExportToWkt()
            print(wkt_stored)
            fields = BackGDALFileVector._fields_of_lyr(lyr)
            type = conv.str_of_wkbgeom(lyr.GetGeomType())

        super(BackGDALFileVector, self).__init__(
            back_ds=back_ds,
            wkt_stored=wkt_stored,
            mode=mode,
            driver=driver,
            open_options=open_options,
            path=path,
            uid=uid,
            layer=layer,
            fields=fields,
            rect=rect,
            type=type
        )

        self._type_of_field_index = [
            conv.type_of_oftstr(field['type'])
            for field in self.fields
        ]

    @staticmethod
    def open_file(path, layer, driver, options, mode):
        """Open a vector datasource"""
        options = [str(arg) for arg in options] if len(options) else []
        gdal_ds = gdal.OpenEx(
            path,
            conv.of_of_mode(mode) | conv.of_of_str('vector'),
            [driver],
            options,
        )
        if gdal_ds is None:
            raise ValueError('Could not open `{}` with `{}` (gdal error: `{}`)'.format(
                path, driver, str(gdal.GetLastErrorMsg()).strip('\n')
            ))
        if layer is None:
            layer = 0
        if isinstance(layer, numbers.Integral):
            lyr = gdal_ds.GetLayer(layer)
        else:
            lyr = gdal_ds.GetLayerByName(layer)
        if lyr is None:
            raise Exception('Could not open layer (gdal error: %s)' % str(gdal.GetLastErrorMsg()).strip('\n'))
        return gdal_ds, lyr

    @contextlib.contextmanager
    def acquire_driver_object(self):
        with self.back_ds.acquire_driver_object(
                self.uid,
                lambda: self.open_file(self.path, self.layer, self.driver, self.open_options, self.mode),
        ) as gdal_objs:
            yield gdal_objs

    def delete(self):
        super(BackGDALFileVector, self).delete()

        dr = gdal.GetDriverByName(self.driver)
        err = dr.Delete(self.path)
        if err:
            raise RuntimeError('Could not delete `{}` (gdal error: `{}`)'.format(
                self.path, str(gdal.GetLastErrorMsg()).strip('\n')
            ))
