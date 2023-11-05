import numpy as np
import PIL
import rembg
from napari.qt.threading import thread_worker
import napari
import napari.layers
from qtpy.QtWidgets import QComboBox, QGridLayout, QWidget, QSizePolicy, QLabel, QPushButton, QProgressBar
from qtpy.QtCore import Qt
from skimage.measure import regionprops_table

def rembg_predict(image: np.ndarray) -> np.ndarray:
    """Binary segmentation using rembg."""
    seg = np.array(rembg.remove(PIL.Image.fromarray(image), post_process_mask=True))
    seg = np.mean(seg, axis=2)
    seg[seg != 0] = 1
    seg = seg.astype(np.uint8)
    return seg

class RemBGWidget(QWidget):
    def __init__(self, napari_viewer):
        super().__init__()
        self.viewer = napari_viewer

        self.image_layer = None
        self.labels_layer = None
        self.shapes_layer = None

        # Layout
        grid_layout = QGridLayout()
        grid_layout.setAlignment(Qt.AlignTop)
        self.setLayout(grid_layout)

        # Image
        self.cb_image = QComboBox()
        self.cb_image.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        grid_layout.addWidget(QLabel("Image (2D / 3D / RGB)", self), 0, 0)
        grid_layout.addWidget(self.cb_image, 0, 1)

        # Result
        self.cb_result = QComboBox()
        self.cb_result.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        grid_layout.addWidget(QLabel("Mask (Labels, optional)", self), 1, 0)
        grid_layout.addWidget(self.cb_result, 1, 1)

        # Region of interest
        self.cb_roi = QComboBox()
        self.cb_roi.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        grid_layout.addWidget(QLabel("ROI (Shapes, optional)", self), 2, 0)
        grid_layout.addWidget(self.cb_roi, 2, 1)

        # Compute button
        self.remove_background_btn = QPushButton("Select foreground", self)
        self.remove_background_btn.clicked.connect(self._trigger_remove_background)
        grid_layout.addWidget(self.remove_background_btn, 3, 0, 1, 2)

        # Progress bar
        self.pbar = QProgressBar(self, minimum=0, maximum=1)
        self.pbar.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        grid_layout.addWidget(self.pbar, 4, 0, 1, 2)

        # Setup layer callbacks
        self.viewer.layers.events.inserted.connect(
            lambda e: e.value.events.name.connect(self._on_layer_change)
        )
        self.viewer.layers.events.inserted.connect(self._on_layer_change)
        self.viewer.layers.events.removed.connect(self._on_layer_change)
        self._on_layer_change(None)

        self.viewer.dims.events.current_step.connect(self._on_slice_change)

    @property
    def image_data(self):
        """The image data, adjusted to handle the RGB case."""
        if self.image_layer is None:
            return
        
        if self.image_layer.data is None:
            return

        return self.image_layer.data
    
    @property
    def is_in_3d_view(self):
        return self.viewer.dims.ndisplay == 3

    @property
    def dims_displayed(self):
        return list(self.viewer.dims.displayed)
    
    @property
    def ndim(self):        
        if self.image_data is None:
            return
        
        if self.image_layer.rgb is True:
            return 2
        else:
            return self.image_layer.data.ndim
    
    @property
    def axes(self):
        if self.is_in_3d_view:
            return
        
        axes = self.dims_displayed
        if self.ndim == 3:
            axes.insert(0, list(set([0, 1, 2]) - set(self.dims_displayed))[0])
        
        return axes
    
    @property
    def current_step(self):
        """Current step, adjusted based on the layer transpose state."""
        return np.array(self.viewer.dims.current_step)[self.axes][0]
    
    @property
    def image_data_slice(self):
        """The currently visible 2D slice if the image is 3D, otherwise the image itself (if 2D)."""      
        if self.image_data is None:
            return
        
        if self.ndim == 2:
            return self.image_data
        
        elif self.ndim == 3:
            return self.image_data.transpose(self.axes)[self.current_step]
    
    @property
    def selected_label(self):
        if self.labels_layer is None:
            return
        
        return self.labels_layer.selected_label
            
    def _on_layer_change(self, e):
        self.cb_image.clear()
        for x in self.viewer.layers:
            if isinstance(x, napari.layers.Image):
                if x.data.ndim in [2, 3]:
                    self.cb_image.addItem(x.name, x.data)
        
        if self.cb_image.currentText() != '':
            self.image_layer = self.viewer.layers[self.cb_image.currentText()]
        
        self.cb_result.clear()
        for x in self.viewer.layers:
            if isinstance(x, napari.layers.Labels):
                self.cb_result.addItem(x.name, x.data)

        if self.cb_result.currentText() != '':
            self.labels_layer = self.viewer.layers[self.cb_result.currentText()]
        
        self.cb_roi.clear()
        for x in self.viewer.layers:
            if isinstance(x, napari.layers.Shapes):
                self.cb_roi.addItem(x.name, x.data)
        
        if self.cb_roi.currentText() != '':
            self.shapes_layer = self.viewer.layers[self.cb_roi.currentText()]

    def _on_slice_change(self, event):
        """In 3D mode, remove the rectangle ROI on slice change."""
        if self.shapes_layer is not None \
            and self.shapes_layer.nshapes == 1 \
            and self.shapes_layer.shape_type[0] == 'rectangle':
                self.shapes_layer.data = []
                self.shapes_layer.refresh()

    @thread_worker
    def _remove_background(self) -> np.ndarray:
        image_data_slice_roi_adjusted = self.image_data_slice.copy()

        if self.shapes_layer is not None \
            and self.shapes_layer.nshapes == 1 \
            and self.shapes_layer.shape_type[0] == 'rectangle' \
            and not (self.ndim == 3) & (self.axes[0] != 0):  # The shapes to_label() don't work in transposed dimensions; see issue #5505

                roi_mask = self.shapes_layer.to_labels().astype(np.uint8)
                
                if self.ndim == 3:
                    roi_mask = np.max(roi_mask.transpose(self.axes), axis=0)
                                
                bbox = regionprops_table(roi_mask, properties=['bbox'])

                x0 = int(bbox['bbox-0'][0])
                y0 = int(bbox['bbox-1'][0])
                x1 = int(bbox['bbox-2'][0])
                y1 = int(bbox['bbox-3'][0])

                segmentation = np.zeros(np.array(image_data_slice_roi_adjusted.shape)[:2], dtype=np.uint8)
                image_data_slice_roi_adjusted = image_data_slice_roi_adjusted[x0:x1, y0:y1]
                segmentation_roi_adjusted = rembg_predict(image_data_slice_roi_adjusted)
                segmentation[x0:x1, y0:y1] = segmentation_roi_adjusted
        else:
            segmentation = rembg_predict(image_data_slice_roi_adjusted)
        
        return segmentation
    
    def _trigger_remove_background(self):
        if self.is_in_3d_view:
            return
        
        if self.cb_image.currentText() == '':
            return
        
        self.image_layer = self.viewer.layers[self.cb_image.currentText()]

        if self.cb_roi.currentText() != '':
            self.shapes_layer = self.viewer.layers[self.cb_roi.currentText()]
        else:
            self.shapes_layer = None

        self.pbar.setMaximum(0)
        worker = self._remove_background()
        worker.returned.connect(self._thread_returned)
        worker.start()

    def _thread_returned(self, segmentation):
        if self.cb_result.currentText() == '':
            if self.image_layer.rgb is True:
                self.labels_layer = self.viewer.add_labels(np.zeros_like(np.mean(self.image_data, axis=2), dtype=np.int_), name='Foreground mask')
            else:
                self.labels_layer = self.viewer.add_labels(np.zeros_like(self.image_data, dtype=np.int_), name='Foreground mask')
        else:
            self.labels_layer = self.viewer.layers[self.cb_result.currentText()]

        mask = segmentation > 0

        if self.ndim == 2:
            self.labels_layer.data[mask] = self.selected_label
        elif self.ndim == 3:
            self.labels_layer.data.transpose(self.axes)[self.current_step][mask] = self.selected_label

        self.labels_layer.refresh()

        self.pbar.setMaximum(1)