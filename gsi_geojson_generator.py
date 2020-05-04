import os
import math
import time
import tempfile
import shutil
import urllib
import glob
import subprocess
import json
import copy

from qgis.PyQt import uic
from qgis.PyQt import QtWidgets
from qgis.PyQt.QtCore import QThread, pyqtSignal
from qgis.core import QgsProject, QgsVectorLayer, QgsDataProvider
import processing

from .exlib import tiletanic
from .exlib.shapely import geometry as shapely_geometry
from . import settings

TMP_PATH = os.path.join(tempfile.gettempdir(), 'vtdownloader')


class GsiGeojsonGenerator(QtWidgets.QDialog):
    def __init__(self, leftbottom_lonlat:list, righttop_lonlat:list, layer_key:str, zoomlevel:int, clipmode=False):
        super().__init__()
        os.makedirs(os.path.join(TMP_PATH), exist_ok=True)
        self.ui = uic.loadUi(os.path.join(os.path.dirname(__file__), 'gsi_geojson_generator_indicator_base.ui'), self)

        self.leftbottom_lonlat = leftbottom_lonlat
        self.righttop_lonlat = righttop_lonlat
        self.layer_key = layer_key
        self.zoomlevel = zoomlevel
        self.clipmode = clipmode

        self.tileindex = self.make_tileindex()

        self.ui.abortPushButton.clicked.connect(self.on_abort_pushbutton_clicked)
        self.ui.download_progressBar.setRange(0, len(self.tileindex))
        self.ui.download_progressBar.setFormat('%v/%m(%p%)')

        self.tile_downloader = TileDownloader(self.tileindex, self.layer_key)
        self.tile_downloader.progressChanged.connect(self.update_download_progress)
        self.tile_downloader.downloadFinished.connect(lambda:self.add_layer_to_proj())

    def run(self):
        self.show()
        self.tile_downloader.start()

    def make_bbox(self):
        leftbottom_as_3857 = self.lonlat_to_webmercator(self.leftbottom_lonlat)
        righttop_as_3857 = self.lonlat_to_webmercator(self.righttop_lonlat)
        xMin = leftbottom_as_3857[0]
        xMax = righttop_as_3857[0]
        yMin = leftbottom_as_3857[1]
        yMax = righttop_as_3857[1]
        return [xMin, xMax, yMin, yMax]

    def make_tileindex(self):
        leftbottom_as_3857 = self.lonlat_to_webmercator(self.leftbottom_lonlat)
        righttop_as_3857 = self.lonlat_to_webmercator(self.righttop_lonlat)
        bbox_geometry = self.make_rectangle_of(leftbottom_as_3857, righttop_as_3857)

        tiler = tiletanic.tileschemes.WebMercator()
        feature_shape = shapely_geometry.shape(bbox_geometry)

        covering_tiles_itr = tiletanic.tilecover.cover_geometry(tiler, feature_shape, self.zoomlevel)
        covering_tiles = []
        for tile in covering_tiles_itr:
            tile_xyz = [tile[0], tile[1], tile[2]]
            covering_tiles.append(tile_xyz)

        return covering_tiles

    def lonlat_to_webmercator(self, lonlat):
        return [lonlat[0] * 20037508.34 / 180,
                math.log(math.tan( (90 + lonlat[1]) * math.pi / 360) ) / (math.pi / 180) * 20037508.34 / 180]

    def make_rectangle_of(self, leftbottom, righttop):
        x1 = leftbottom[0]
        y1 = leftbottom[1]
        x2 = righttop[0]
        y2 = righttop[1]
        rectangle = {
            'type':'Polygon',
            'coordinates':[
                [
                    [x1, y1], [x2, y1],
                    [x2, y2], [x1, y2], [x1, y1]
                ]
            ]
        }
        return rectangle

    def update_download_progress(self, value:int):
        self.ui.download_progressBar.setValue(value)

    def add_layer_to_proj(self):
        vlayer = self.tile_downloader.mergedlayer
        
        if self.clipmode:
            bbox = self.make_bbox()
            vlayer = self.clip_vlayer(bbox, vlayer)
        
        vlayer.setName(self.layer_key)
        QgsProject.instance().addMapLayer(vlayer)
        QtWidgets.QMessageBox.information(None, 'GSI-VTDownloader', 'Completed')
        self.close()

    def clip_vlayer(self, bbox, vlayer:QgsVectorLayer)->QgsVectorLayer:
        cliped = processing.run('qgis:extractbyextent', {
            'INPUT':vlayer,
            'CLIP':False,
            'EXTENT':'%s,%s,%s,%s'%(bbox[0],
                                    bbox[1], 
                                    bbox[2], 
                                    bbox[3]),
            'OUTPUT':'memory:'
        })['OUTPUT']
        return cliped

    def on_abort_pushbutton_clicked(self):
        self.tile_downloader.quit()
        QtWidgets.QMessageBox.information(None, 'GSI-VTDownloader', '処理を中止しました')
        self.close()


class TileDownloader(QThread):
    TMP_PATH = os.path.join(tempfile.gettempdir(), 'vtdownloader')
    TILE_URL = r'https://cyberjapandata.gsi.go.jp/xyz/experimental_bvmap/{z}/{x}/{y}.pbf'
    progressChanged = pyqtSignal(int)
    downloadFinished = pyqtSignal(bool)

    def __init__(self, tileindex, layer_key):
        super().__init__()
        self.tileindex = tileindex
        self.layer_key = layer_key
        self.mergedlayer = None

    def run(self):
        self.make_xyz_dirs()

        pbfuris = []
        for i in range(len(self.tileindex)):
            xyz = self.tileindex[i]
            x = str(xyz[0])
            y = str(xyz[1])
            z = str(xyz[2])
            current_tileurl = self.TILE_URL
            current_tileurl = current_tileurl.replace(r'{z}', z).replace(r'{x}', x).replace(r'{y}', y)
            target_path = os.path.join(self.TMP_PATH, z, x, y + '.pbf')

            #download New file only
            if not os.path.exists(target_path):
                pbfdata = urllib.request.urlopen(current_tileurl).read()
                with open(target_path, mode='wb') as f:
                    f.write(pbfdata)

            SOURCE_LAYERS = settings.SOURCE_LAYERS
            geometrytype = self.translate_gsitype_to_geometry(SOURCE_LAYERS[self.layer_key]['datatype'])
            pbfuri = target_path + '|layername=' + self.layer_key + '|geometrytype=' + geometrytype
            pbflayer = QgsVectorLayer(pbfuri, 'pbf', 'ogr')
            pbfprovider = pbflayer.dataProvider()

            if not pbfprovider.isValid():
                continue

            pbfuris.append(pbfuri)

            self.progressChanged.emit(i + 1)
        
        mergedlayer_shp = processing.run('saga:mergevectorlayers', {
            'INPUT':pbfuris,
            'MATCH':True,
            'MERGED':'TEMPORARY_OUTPUT',
            'SRCINFO':True
        })['MERGED']
        #always 3857
        self.mergedlayer = QgsVectorLayer(mergedlayer_shp, self.layer_key, 'ogr')

        self.downloadFinished.emit(True)

    def make_xyz_dirs(self):
        for xyz in self.tileindex:
            x = str(xyz[0])
            z = str(xyz[2])
            os.makedirs(os.path.join(self.TMP_PATH, z, x), exist_ok=True)

    def translate_gsitype_to_geometry(self, gsitype):
        if gsitype == '点':
            return 'Point'
        elif gsitype == '線':
            return 'LineString'
        else:
            return 'Polygon'
