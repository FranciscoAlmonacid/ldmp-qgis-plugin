# -*- coding: utf-8 -*-
"""
/***************************************************************************
 LDMP - A QGIS plugin
 This plugin supports monitoring and reporting of land degradation to the UNCCD 
 and in support of the SDG Land Degradation Neutrality (LDN) target.
                              -------------------
        begin                : 2017-05-23
        git sha              : $Format:%H$
        copyright            : (C) 2017 by Conservation International
        email                : GEF-LDMP@conservation.org
 ***************************************************************************/
"""

import os
import json

from datetime import date, datetime
from math import floor, log10

import numpy as np 
from osgeo import gdal 

from PyQt4 import QtGui
from PyQt4.QtCore import QSettings, QDate, QAbstractTableModel, Qt

from qgis.core import QgsColorRampShader, QgsRasterShader, QgsSingleBandPseudoColorRenderer, QgsRasterBandStats

from qgis.utils import iface
mb = iface.messageBar()

from qgis.gui import QgsMessageBar

from LDMP.gui.DlgJobs import Ui_DlgJobs
from LDMP.gui.DlgJobsDetails import Ui_DlgJobsDetails
from LDMP.plot import DlgPlot

from LDMP import log
from LDMP.download import Download, check_goog_cloud_store_hash
from LDMP.api import get_script, get_user_email, get_execution

def json_serial(obj):
    """JSON serializer for objects not serializable by default json code"""
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    raise TypeError("Type %s not serializable" % type(obj))

def create_json_metadata(job, outfile):
    outfile = os.path.splitext(outfile)[0] + '.json'
    with open(outfile, 'w') as outfile:
        json.dump(job['raw'], outfile, default=json_serial, sort_keys=True, 
                indent=4, separators=(',', ': '))

def get_scripts():
    scripts = get_script()
    if not scripts:
        return None
    # The scripts endpoint lists scripts in a list of dictionaries. Convert 
    # this to a dictionary keyed by script id
    scripts_dict = {}
    for script in scripts:
        script_id = script.pop('id')
        scripts_dict[script_id] = script
    return scripts_dict

def round_to_n(x, sf=3):
    'Function to round a positive value to n significant figures'
    return round(x, -int(floor(log10(x))) + (sf - 1))

def get_extreme(mn, mx, sf=3):
    'Function to get rounded extreme value for a centered colorbar'
    return max([round_to_n(abs(mn), sf), round_to_n(abs(mx), sf)])

class DlgJobs(QtGui.QDialog, Ui_DlgJobs):
    def __init__(self, parent=None):
        """Constructor."""
        super(DlgJobs, self).__init__(parent)

        self.settings = QSettings()

        self.setupUi(self)

        # Set a variable used to record the necessary window width to view all 
        # columns
        self._full_width = None

        self.bar = QgsMessageBar()
        self.bar.setSizePolicy(QtGui.QSizePolicy.Minimum, QtGui.QSizePolicy.Fixed)
        self.layout().addWidget(self.bar, 0, 0, Qt.AlignTop)

        jobs_cache = self.settings.value("LDMP/jobs_cache", None)
        if jobs_cache:
            self.jobs = jobs_cache
            self.update_jobs_table()

        self.refresh.clicked.connect(self.btn_refresh)
        self.download.clicked.connect(self.btn_download)

        # Only enable download button if a job is selected
        self.download.setEnabled(False)

    def resizeWindowToColumns(self):
        if not self._full_width:
            margins = self.layout().contentsMargins()
            self._full_width = margins.left() + margins.right() + \
                self.jobs_view.frameWidth() * 2 + \
                self.jobs_view.verticalHeader().width() + \
                self.jobs_view.horizontalHeader().length() + \
                self.jobs_view.style().pixelMetric(QtGui.QStyle.PM_ScrollBarExtent)
        self.resize(self._full_width, self.height())

    def selection_changed(self):
        if not self.jobs_view.selectedIndexes():
            self.download.setEnabled(False)
        else:
            rows = list(set(index.row() for index in self.jobs_view.selectedIndexes()))
            for row in rows:
                # Don't set button to enabled if any of the tasks aren't yet 
                # finished
                if self.jobs[row]['status'] != 'FINISHED':
                    self.download.setEnabled(False)
                    return
            self.download.setEnabled(True)

    def btn_refresh(self):
        email = get_user_email()
        if email:
            self.jobs = get_execution()
            if self.jobs:
                # Add script names and descriptions to jobs list
                self.scripts = get_scripts()
                if not self.scripts:
                    return False
                for job in self.jobs:
                    # self.jobs will have prettified data for usage in table, 
                    # so save a backup of the original data under key 'raw'
                    job['raw'] = job.copy()
                    script = job.get('script_id', None)
                    if script:
                        job['script_name'] = self.scripts[job['script_id']]['name']
                        job['script_description'] = self.scripts[job['script_id']]['description']
                    else:
                        # Handle case of scripts that have been removed or that are 
                        # no longer supported
                        job['script_name'] =  self.tr('Script not found')
                        job['script_description'] = self.tr('Script not found')

                # Pretty print dates and pull the metadata sent as input params
                for job in self.jobs:
                    job['start_date'] = datetime.strftime(job['start_date'], '%Y/%m/%d (%H:%M)')
                    job['end_date'] = datetime.strftime(job['end_date'], '%Y/%m/%d (%H:%M)')
                    job['task_name'] = job['params'].get('task_name', '')
                    job['task_notes'] = job['params'].get('task_notes', '')
                    job['params'] = job['params']

                # Cache jobs for later reuse
                self.settings.setValue("LDMP/jobs_cache", self.jobs)

                self.update_jobs_table()

                return True
        return False

    def update_jobs_table(self):
        if self.jobs:
            table_model = JobsTableModel(self.jobs, self)
            proxy_model = QtGui.QSortFilterProxyModel()
            proxy_model.setSourceModel(table_model)
            self.jobs_view.setModel(proxy_model)

            # Add "Notes" buttons in cell
            for row in range(0, len(self.jobs)):
                btn = QtGui.QPushButton(self.tr("Details"))
                btn.clicked.connect(self.btn_details)
                self.jobs_view.setIndexWidget(proxy_model.index(row, 5), btn)

            self.jobs_view.horizontalHeader().setResizeMode(QtGui.QHeaderView.ResizeToContents)
            self.jobs_view.setSelectionBehavior(QtGui.QAbstractItemView.SelectRows)
            self.jobs_view.selectionModel().selectionChanged.connect(self.selection_changed)

            self.resizeWindowToColumns()


    def btn_details(self):
        button = self.sender()
        index = self.jobs_view.indexAt(button.pos())

        details_dlg = DlgJobsDetails()

        job = self.jobs[index.row()]

        details_dlg.task_name.setText(job.get('task_name', ''))
        details_dlg.task_status.setText(job.get('status', ''))
        details_dlg.comments.setText(job.get('task_notes', ''))
        details_dlg.input.setText(json.dumps(job.get('params', ''), indent=4, sort_keys=True))
        details_dlg.output.setText(json.dumps(job.get('results', ''), indent=4, sort_keys=True))

        details_dlg.show()
        details_dlg.exec_()

    def btn_download(self):
        rows = list(set(index.row() for index in self.jobs_view.selectedIndexes()))
        # Check if we need a download directory - some tasks don't need to save 
        # data, but if any of the chosen \tasks do, then we need to choose a 
        # folder.
        need_dir = False
        for row in rows:
            job = self.jobs[row]
            if job['results'].get('type') != 'timeseries':
                need_dir = True
                break

        if need_dir:
            download_dir = QtGui.QFileDialog.getExistingDirectory(self, 
                    self.tr("Directory to save files"),
                    self.settings.value("LDMP/download_dir", None),
                    QtGui.QFileDialog.ShowDirsOnly)
            if download_dir:
                if os.access(download_dir, os.W_OK):
                    self.settings.setValue("LDMP/download_dir", download_dir)
                    log("Downloading results to {}".format(download_dir))
                else:
                    QtGui.QMessageBox.critical(None, self.tr("Error"),
                            self.tr("Cannot write to {}. Choose a different folder.".format(download_dir), None))
                    return False
            else:
                return False

        self.close()

        for row in rows:
            job = self.jobs[row]
            log("Processing job {}".format(job))
            if job['results'].get('type') == 'productivity_trajectory':
                download_prod_traj(job, download_dir)
            elif job['results'].get('type') == 'productivity_state':
                download_prod_state(job, download_dir)
            elif job['results'].get('type') == 'productivity_performance':
                download_prod_perf(job, download_dir)
            elif job['results'].get('type') == 'land_cover':
                download_land_cover(job, download_dir)
            elif job['results'].get('type') == 'timeseries':
                download_timeseries(job)
            else:
                raise ValueError("Unrecognized result type in download results: {}".format(dataset['dataset']))

class DlgJobsDetails(QtGui.QDialog, Ui_DlgJobsDetails):
    def __init__(self, parent=None):
        """Constructor."""
        super(DlgJobsDetails, self).__init__(parent)

        self.setupUi(self)

class JobsTableModel(QAbstractTableModel):
    def __init__(self, datain, parent=None, *args):
        QAbstractTableModel.__init__(self, parent, *args)
        self.jobs = datain

        # Column names as tuples with json name in [0], pretty name in [1]
        # Note that the columns with json names set to to INVALID aren't loaded 
        # into the shell, but shown from a widget.
        colname_tuples = [('task_name', QtGui.QApplication.translate('LDMPPlugin', 'Task name')),
                          ('script_name', QtGui.QApplication.translate('LDMPPlugin', 'Job')),
                          ('start_date', QtGui.QApplication.translate('LDMPPlugin', 'Start time')),
                          ('end_date', QtGui.QApplication.translate('LDMPPlugin', 'End time')),
                          ('status', QtGui.QApplication.translate('LDMPPlugin', 'Status')),
                          ('INVALID', QtGui.QApplication.translate('LDMPPlugin', 'Details'))]
        self.colnames_pretty = [x[1] for x in colname_tuples]
        self.colnames_json = [x[0] for x in colname_tuples]

    def rowCount(self, parent):
        return len(self.jobs)

    def columnCount(self, parent):
        return len(self.colnames_json)

    def data(self, index, role):
        if not index.isValid():
            return None
        elif role != Qt.DisplayRole:
            return None
        return self.jobs[index.row()].get(self.colnames_json[index.column()], '')

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if role == Qt.DisplayRole and orientation == Qt.Horizontal:
            return self.colnames_pretty[section]
        return QAbstractTableModel.headerData(self, section, orientation, role)

def download_result(url, outfile, job):
    log("Downloading {}".format(url))
    #TODO: Check if this file was already downloaded
    worker = Download(url, outfile)
    worker.start()
    if worker.get_resp():
        create_json_metadata(job, outfile)
        check_goog_cloud_store_hash(url, outfile)
        return True
    else:
        return None

def download_land_cover(job, download_dir):
    log("downloading land_cover results...")
    for dataset in job['results'].get('datasets'):
        for url in dataset.get('urls'):
            outfile = os.path.join(download_dir, url['url'].rsplit('/', 1)[-1])
            resp = download_result(url['url'], outfile, job)
            if not resp:
                return
            if dataset['dataset'] == 'lc_baseline':
                style_land_cover_lc_baseline(outfile)
            elif dataset['dataset'] == 'lc_target':
                style_land_cover_lc_target(outfile)
            elif dataset['dataset'] == 'lc_change':
                style_land_cover_lc_change(outfile)
            elif dataset['dataset'] == 'land_deg':
                style_land_cover_land_deg(outfile)
            else:
                raise ValueError("Unrecognized dataset type in download results: {}".format(dataset['dataset']))

def style_land_cover_lc_baseline(outfile):
    layer_lc_baseline = iface.addRasterLayer(outfile, QtGui.QApplication.translate('LDMPPlugin', 'Land cover (baseline)'))
    if not layer_lc_baseline.isValid():
        log('Failed to add layer')
        return None
    fcn = QgsColorRampShader()
    fcn.setColorRampType(QgsColorRampShader.EXACT)
    lst = [QgsColorRampShader.ColorRampItem(1, QtGui.QColor('#a50f15'), QtGui.QApplication.translate('LDMPPlugin', 'Cropland')),
           QgsColorRampShader.ColorRampItem(2, QtGui.QColor('#006d2c'), QtGui.QApplication.translate('LDMPPlugin', 'Forest land')),
           QgsColorRampShader.ColorRampItem(3, QtGui.QColor('#d8d800'), QtGui.QApplication.translate('LDMPPlugin', 'Grassland')),
           QgsColorRampShader.ColorRampItem(4, QtGui.QColor('#08519c'), QtGui.QApplication.translate('LDMPPlugin', 'Wetlands')),
           QgsColorRampShader.ColorRampItem(5, QtGui.QColor('#54278f'), QtGui.QApplication.translate('LDMPPlugin', 'Settlements')),
           QgsColorRampShader.ColorRampItem(6, QtGui.QColor('#252525'), QtGui.QApplication.translate('LDMPPlugin', 'Other land'))]
    fcn.setColorRampItemList(lst)
    shader = QgsRasterShader()
    shader.setRasterShaderFunction(fcn)
    pseudoRenderer = QgsSingleBandPseudoColorRenderer(layer_lc_baseline.dataProvider(), 1, shader)
    layer_lc_baseline.setRenderer(pseudoRenderer)
    layer_lc_baseline.triggerRepaint()
    iface.legendInterface().refreshLayerSymbology(layer_lc_baseline)

def style_land_cover_lc_target(outfile):
    layer_lc_target = iface.addRasterLayer(outfile, QtGui.QApplication.translate('LDMPPlugin', 'Land cover (target)'))
    if not layer_lc_target.isValid():
        log('Failed to add layer')
        return None
    fcn = QgsColorRampShader()
    fcn.setColorRampType(QgsColorRampShader.EXACT)
    lst = [QgsColorRampShader.ColorRampItem(1, QtGui.QColor('#a50f15'), QtGui.QApplication.translate('LDMPPlugin', 'Cropland')),
           QgsColorRampShader.ColorRampItem(2, QtGui.QColor('#006d2c'), QtGui.QApplication.translate('LDMPPlugin', 'Forest land')),
           QgsColorRampShader.ColorRampItem(3, QtGui.QColor('#d8d800'), QtGui.QApplication.translate('LDMPPlugin', 'Grassland')),
           QgsColorRampShader.ColorRampItem(4, QtGui.QColor('#08519c'), QtGui.QApplication.translate('LDMPPlugin', 'Wetlands')),
           QgsColorRampShader.ColorRampItem(5, QtGui.QColor('#54278f'), QtGui.QApplication.translate('LDMPPlugin', 'Settlements')),
           QgsColorRampShader.ColorRampItem(6, QtGui.QColor('#252525'), QtGui.QApplication.translate('LDMPPlugin', 'Other land'))]
    fcn.setColorRampItemList(lst)
    shader = QgsRasterShader()
    shader.setRasterShaderFunction(fcn)
    pseudoRenderer = QgsSingleBandPseudoColorRenderer(layer_lc_target.dataProvider(), 1, shader)
    layer_lc_target.setRenderer(pseudoRenderer)
    layer_lc_target.triggerRepaint()
    iface.legendInterface().refreshLayerSymbology(layer_lc_target)

def style_land_cover_lc_change(outfile):
    layer_lc_change = iface.addRasterLayer(outfile, QtGui.QApplication.translate('LDMPPlugin', 'Land cover change'))
    if not layer_lc_change.isValid():
        log('Failed to add layer')
        return None
    fcn = QgsColorRampShader()
    fcn.setColorRampType(QgsColorRampShader.EXACT)
    lst = [QgsColorRampShader.ColorRampItem(11, QtGui.QColor(246, 246, 234), 'Croplands-Croplands'),
           QgsColorRampShader.ColorRampItem(12, QtGui.QColor('#de2d26'), 'Croplands-Forest land'),
           QgsColorRampShader.ColorRampItem(13, QtGui.QColor('#fb6a4a'), 'Croplands-Grassland'),
           QgsColorRampShader.ColorRampItem(14, QtGui.QColor('#fc9272'), 'Croplands-Wetlands'),
           QgsColorRampShader.ColorRampItem(15, QtGui.QColor('#fcbba1'), 'Croplands-Settlements'),
           QgsColorRampShader.ColorRampItem(16, QtGui.QColor('#fee5d9'), 'Croplands-Other land'),
           QgsColorRampShader.ColorRampItem(22, QtGui.QColor(246, 246, 234), 'Forest land-Forest land'),
           QgsColorRampShader.ColorRampItem(21, QtGui.QColor('#31a354'), 'Forest land-Croplands'),
           QgsColorRampShader.ColorRampItem(23, QtGui.QColor('#74c476'), 'Forest land-Grassland'),
           QgsColorRampShader.ColorRampItem(24, QtGui.QColor('#a1d99b'), 'Forest land-Wetlands'),
           QgsColorRampShader.ColorRampItem(25, QtGui.QColor('#c7e9c0'), 'Forest land-Settlements'),
           QgsColorRampShader.ColorRampItem(26, QtGui.QColor('#edf8e9'), 'Forest land-Other land'),
           QgsColorRampShader.ColorRampItem(33, QtGui.QColor(246, 246, 234), 'Grassland-Grassland'),
           QgsColorRampShader.ColorRampItem(31, QtGui.QColor('#727200'), 'Grassland-Croplands'),
           QgsColorRampShader.ColorRampItem(32, QtGui.QColor('#8b8b00'), 'Grassland-Forest land'),
           QgsColorRampShader.ColorRampItem(34, QtGui.QColor('#a5a500'), 'Grassland-Wetlands'),
           QgsColorRampShader.ColorRampItem(35, QtGui.QColor('#bebe00'), 'Grassland-Settlements'),
           QgsColorRampShader.ColorRampItem(36, QtGui.QColor('#d8d800'), 'Grassland-Other land'),
           QgsColorRampShader.ColorRampItem(44, QtGui.QColor(246, 246, 234), 'Wetlands-Wetlands'),
           QgsColorRampShader.ColorRampItem(41, QtGui.QColor('#3182bd'), 'Wetlands-Croplands'),
           QgsColorRampShader.ColorRampItem(42, QtGui.QColor('#6baed6'), 'Wetlands-Forest land'),
           QgsColorRampShader.ColorRampItem(43, QtGui.QColor('#9ecae1'), 'Wetlands-Grassland'),
           QgsColorRampShader.ColorRampItem(45, QtGui.QColor('#c6dbef'), 'Wetlands-Settlements'),
           QgsColorRampShader.ColorRampItem(46, QtGui.QColor('#eff3ff'), 'Wetlands-Other land'),
           QgsColorRampShader.ColorRampItem(55, QtGui.QColor(246, 246, 234), 'Settlements-Settlements'),
           QgsColorRampShader.ColorRampItem(51, QtGui.QColor('#756bb1'), 'Settlements-Croplands'),
           QgsColorRampShader.ColorRampItem(52, QtGui.QColor('#9e9ac8'), 'Settlements-Forest land'),
           QgsColorRampShader.ColorRampItem(53, QtGui.QColor('#bcbddc'), 'Settlements-Grassland'),
           QgsColorRampShader.ColorRampItem(54, QtGui.QColor('#dadaeb'), 'Settlements-Wetlands'),
           QgsColorRampShader.ColorRampItem(56, QtGui.QColor('#f2f0f7'), 'Settlements-Other land'),
           QgsColorRampShader.ColorRampItem(66, QtGui.QColor(246, 246, 234), 'Other land-Other land'),
           QgsColorRampShader.ColorRampItem(61, QtGui.QColor('#636363'), 'Other land-Croplands'),
           QgsColorRampShader.ColorRampItem(62, QtGui.QColor('#969696'), 'Other land-Forest land'),
           QgsColorRampShader.ColorRampItem(63, QtGui.QColor('#bdbdbd'), 'Other land-Grassland'),
           QgsColorRampShader.ColorRampItem(64, QtGui.QColor('#d9d9d9'), 'Other land-Wetlands'),
           QgsColorRampShader.ColorRampItem(65, QtGui.QColor('#f7f7f7'), 'Other land-Settlements')]
    fcn.setColorRampItemList(lst)
    shader = QgsRasterShader()
    shader.setRasterShaderFunction(fcn)
    pseudoRenderer = QgsSingleBandPseudoColorRenderer(layer_lc_change.dataProvider(), 1, shader)
    layer_lc_change.setRenderer(pseudoRenderer)
    layer_lc_change.triggerRepaint()
    layer_lc_change.triggerRepaint()
    iface.legendInterface().refreshLayerSymbology(layer_lc_change)

def style_land_cover_land_deg(outfile):
    layer_deg = iface.addRasterLayer(outfile, QtGui.QApplication.translate('LDMPPlugin', 'Land cover (degradation)'))
    if not layer_deg.isValid():
        log('Failed to add layer')
        return None
    fcn = QgsColorRampShader()
    fcn.setColorRampType(QgsColorRampShader.EXACT)
    #TODO The GPG doesn't seem to allow for possibility of improvement...?
    lst = [QgsColorRampShader.ColorRampItem(-1, QtGui.QColor(153, 51, 4), QtGui.QApplication.translate('LDMPPlugin', 'Degradation')),
           QgsColorRampShader.ColorRampItem(0, QtGui.QColor(246, 246, 234), QtGui.QApplication.translate('LDMPPlugin', 'Stable')),
           QgsColorRampShader.ColorRampItem(1, QtGui.QColor(0, 140, 121), QtGui.QApplication.translate('LDMPPlugin', 'Improvement'))]
    fcn.setColorRampItemList(lst)
    shader = QgsRasterShader()
    shader.setRasterShaderFunction(fcn)
    pseudoRenderer = QgsSingleBandPseudoColorRenderer(layer_deg.dataProvider(), 1, shader)
    layer_deg.setRenderer(pseudoRenderer)
    layer_deg.triggerRepaint()
    iface.legendInterface().refreshLayerSymbology(layer_deg)

def download_prod_traj(job, download_dir):
    log("Downloading productivity_trajectory results...")
    for dataset in job['results'].get('datasets'):
        for url in dataset.get('urls'):
            if dataset['dataset'] in ['ndvi_trend', 'ue', 'p_restrend']:
                #TODO style layer and set layer name based on the info in the dataset json file
                outfile = os.path.join(download_dir, url['url'].rsplit('/', 1)[-1])
                resp = download_result(url['url'], outfile, job)
                if not resp:
                    return
                style_prod_traj_trend(outfile)
                style_prod_traj_signif(outfile)
            else:
                raise ValueError("Unrecognized dataset type in download results: {}".format(dataset['dataset']))

def style_prod_traj_trend(outfile):
    # Trends layer
    layer_ndvi = iface.addRasterLayer(outfile, QtGui.QApplication.translate('LDMPPlugin', 'Productivity trajectory trend\n(slope of NDVI * 10000)'))
    if not layer_ndvi.isValid():
        log('Failed to add layer')
        return None
    provider = layer_ndvi.dataProvider()

    # Set a colormap centred on zero, going to the extreme value significant to 
    # three figures (after a 2 percent stretch)
    ds = gdal.Open(outfile) 
    band1 = np.array(ds.GetRasterBand(1).ReadAsArray()) 
    band1[band1 >=9997] = 0
    ds = None
    cutoffs = np.percentile(band1, [2, 98])
    extreme = get_extreme(cutoffs[0], cutoffs[1])

    fcn = QgsColorRampShader()
    fcn.setColorRampType(QgsColorRampShader.INTERPOLATED)
    lst = [QgsColorRampShader.ColorRampItem(-extreme, QtGui.QColor(153, 51, 4), QtGui.QApplication.translate('LDMPPlugin', '-{} (declining)').format(extreme)),
           QgsColorRampShader.ColorRampItem(0, QtGui.QColor(246, 246, 234), QtGui.QApplication.translate('LDMPPlugin', '0 (stable)')),
           QgsColorRampShader.ColorRampItem(extreme, QtGui.QColor(0, 140, 121), QtGui.QApplication.translate('LDMPPlugin', '{} (increasing)').format(extreme)),
           QgsColorRampShader.ColorRampItem(9997, QtGui.QColor(0, 0, 0), QtGui.QApplication.translate('LDMPPlugin', 'No data')),
           QgsColorRampShader.ColorRampItem(9998, QtGui.QColor(58, 77, 214), QtGui.QApplication.translate('LDMPPlugin', 'Water')),
           QgsColorRampShader.ColorRampItem(9999, QtGui.QColor(192, 105, 223), QtGui.QApplication.translate('LDMPPlugin', 'Urban land cover'))]
    fcn.setColorRampItemList(lst)
    shader = QgsRasterShader()
    shader.setRasterShaderFunction(fcn)
    pseudoRenderer = QgsSingleBandPseudoColorRenderer(layer_ndvi.dataProvider(), 1, shader)
    layer_ndvi.setRenderer(pseudoRenderer)
    layer_ndvi.triggerRepaint()
    iface.legendInterface().refreshLayerSymbology(layer_ndvi)

def style_prod_traj_signif(outfile):
    # Significance layer
    layer_signif = iface.addRasterLayer(outfile, QtGui.QApplication.translate('LDMPPlugin', 'Productivity trajectory trend (significance)'))
    if not layer_signif.isValid():
        log('Failed to add layer')
        return None
    fcn = QgsColorRampShader()
    fcn.setColorRampType(QgsColorRampShader.EXACT)
    lst = [QgsColorRampShader.ColorRampItem(-1, QtGui.QColor(153, 51, 4), QtGui.QApplication.translate('LDMPPlugin', 'Significant decrease')),
           QgsColorRampShader.ColorRampItem(0, QtGui.QColor(246, 246, 234), QtGui.QApplication.translate('LDMPPlugin', 'No significant change')),
           QgsColorRampShader.ColorRampItem(1, QtGui.QColor(0, 140, 121), QtGui.QApplication.translate('LDMPPlugin', 'Significant increase')),
           QgsColorRampShader.ColorRampItem(2, QtGui.QColor(58, 77, 214), QtGui.QApplication.translate('LDMPPlugin', 'Water')),
           QgsColorRampShader.ColorRampItem(3, QtGui.QColor(192, 105, 223), QtGui.QApplication.translate('LDMPPlugin', 'Urban land cover'))]
    fcn.setColorRampItemList(lst)
    shader = QgsRasterShader()
    shader.setRasterShaderFunction(fcn)
    pseudoRenderer = QgsSingleBandPseudoColorRenderer(layer_signif.dataProvider(), 2, shader)
    layer_signif.setRenderer(pseudoRenderer)
    layer_signif.triggerRepaint()
    iface.legendInterface().refreshLayerSymbology(layer_signif)

def download_prod_state(job, download_dir):
    log("downloading productivity_state results...")
    for dataset in job['results'].get('datasets'):
        for url in dataset.get('urls'):
            #TODO style layer and set layer name based on the info in the dataset json file
            outfile = os.path.join(download_dir, url['url'].rsplit('/', 1)[-1])
            resp = download_result(url['url'], outfile, job)
            if not resp:
                return
            if dataset['dataset'] == 'ini_degr':
                style_prod_state_init(outfile)
            elif dataset['dataset'] == 'eme_degr':
                style_prod_state_emerg(outfile)
            else:
                raise ValueError("Unrecognized dataset type in download results: {}".format(dataset['dataset']))

def style_prod_state_init(outfile):
    # Significance layer
    layer = iface.addRasterLayer(outfile, QtGui.QApplication.translate('LDMPPlugin', 'Productivity state (initial)'))
    if not layer.isValid():
        return None
    fcn = QgsColorRampShader()
    fcn.setColorRampType(QgsColorRampShader.EXACT)
    lst = [QgsColorRampShader.ColorRampItem(-2, QtGui.QColor(0, 0, 0), QtGui.QApplication.translate('LDMPPlugin', 'No data')),
           QgsColorRampShader.ColorRampItem(-1, QtGui.QColor(153, 51, 4), QtGui.QApplication.translate('LDMPPlugin', 'Potentially degraded')),
           QgsColorRampShader.ColorRampItem(0, QtGui.QColor(246, 246, 234), QtGui.QApplication.translate('LDMPPlugin', 'Stable')),
           QgsColorRampShader.ColorRampItem(2, QtGui.QColor(58, 77, 214), QtGui.QApplication.translate('LDMPPlugin', 'Water')),
           QgsColorRampShader.ColorRampItem(3, QtGui.QColor(192, 105, 223), QtGui.QApplication.translate('LDMPPlugin', 'Urban land cover'))]
    fcn.setColorRampItemList(lst)
    shader = QgsRasterShader()
    shader.setRasterShaderFunction(fcn)
    pseudoRenderer = QgsSingleBandPseudoColorRenderer(layer.dataProvider(), 1, shader)
    layer.setRenderer(pseudoRenderer)
    layer.triggerRepaint()
    iface.legendInterface().refreshLayerSymbology(layer)

def style_prod_state_emerg(outfile):
    # Significance layer
    layer = iface.addRasterLayer(outfile, QtGui.QApplication.translate('LDMPPlugin', 'Productivity state (emerging)'))
    if not layer.isValid():
        return None
    fcn = QgsColorRampShader()
    fcn.setColorRampType(QgsColorRampShader.EXACT)
    lst = [QgsColorRampShader.ColorRampItem(-2, QtGui.QColor(0, 0, 0), QtGui.QApplication.translate('LDMPPlugin', 'No data')),
           QgsColorRampShader.ColorRampItem(-1, QtGui.QColor(153, 51, 4), QtGui.QApplication.translate('LDMPPlugin', 'Significant decrease')),
           QgsColorRampShader.ColorRampItem(0, QtGui.QColor(246, 246, 234), QtGui.QApplication.translate('LDMPPlugin', 'No significant change')),
           QgsColorRampShader.ColorRampItem(1, QtGui.QColor(0, 140, 121), QtGui.QApplication.translate('LDMPPlugin', 'Significant increase')),
           QgsColorRampShader.ColorRampItem(2, QtGui.QColor(58, 77, 214), QtGui.QApplication.translate('LDMPPlugin', 'Water')),
           QgsColorRampShader.ColorRampItem(3, QtGui.QColor(192, 105, 223), QtGui.QApplication.translate('LDMPPlugin', 'Urban land cover'))]
    fcn.setColorRampItemList(lst)
    shader = QgsRasterShader()
    shader.setRasterShaderFunction(fcn)
    pseudoRenderer = QgsSingleBandPseudoColorRenderer(layer.dataProvider(), 1, shader)
    layer.setRenderer(pseudoRenderer)
    layer.triggerRepaint()
    iface.legendInterface().refreshLayerSymbology(layer)

def download_prod_perf(job, download_dir):
    log("downloading productivity_perf results...")
    for dataset in job['results'].get('datasets'):
        for url in dataset.get('urls'):
            if dataset['dataset'] == 'productivity_performance':
                #TODO style layer and set layer name based on the info in the dataset json file
                outfile = os.path.join(download_dir, url['url'].rsplit('/', 1)[-1])
                resp = download_result(url['url'], outfile, job)
                if not resp:
                    return
                style_prod_perf(outfile)
            else:
                raise ValueError("Unrecognized dataset type in download results: {}".format(dataset['dataset']))

def style_prod_perf(outfile):
    layer_perf = iface.addRasterLayer(outfile, QtGui.QApplication.translate('LDMPPlugin', 'Productivity performance (degradation)'))
    if not layer_perf.isValid():
        log('Failed to add layer')
        return None
    fcn = QgsColorRampShader()
    fcn.setColorRampType(QgsColorRampShader.EXACT)
    #TODO The GPG doesn't seem to allow for possibility of improvement...?
    lst = [QgsColorRampShader.ColorRampItem(-1, QtGui.QColor(153, 51, 4), QtGui.QApplication.translate('LDMPPlugin', 'Degradation')),
           QgsColorRampShader.ColorRampItem(0, QtGui.QColor(246, 246, 234), QtGui.QApplication.translate('LDMPPlugin', 'Stable')),
           QgsColorRampShader.ColorRampItem(1, QtGui.QColor(0, 140, 121), QtGui.QApplication.translate('LDMPPlugin', 'Improvement')),
           QgsColorRampShader.ColorRampItem(2, QtGui.QColor(58, 77, 214), QtGui.QApplication.translate('LDMPPlugin', 'Water')),
           QgsColorRampShader.ColorRampItem(3, QtGui.QColor(192, 105, 223), QtGui.QApplication.translate('LDMPPlugin', 'Urban land cover'))]
    fcn.setColorRampItemList(lst)
    shader = QgsRasterShader()
    shader.setRasterShaderFunction(fcn)
    pseudoRenderer = QgsSingleBandPseudoColorRenderer(layer_perf.dataProvider(), 1, shader)
    layer_perf.setRenderer(pseudoRenderer)
    layer_perf.triggerRepaint()
    iface.legendInterface().refreshLayerSymbology(layer_perf)

def download_timeseries(job):
    log("processing timeseries results...")
    table = job['results'].get('table', None)
    if not table:
        return None
    data = [x for x in table if x['name'] == 'mean'][0]
    dlg_plot = DlgPlot()
    dlg_plot.plot_data(data['time'], data['y'], job['task_name'])
    dlg_plot.show()
    dlg_plot.exec_()
