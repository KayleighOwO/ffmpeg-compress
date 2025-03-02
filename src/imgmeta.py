#!/usr/bin/env python3

from PyQt6.QtWidgets import *
from PyQt6.QtCore import Qt
from PIL import Image, ExifTags, PngImagePlugin, TiffImagePlugin
import sys
from compress import *

class DragDropWidget(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Video Utils")
        self.setGeometry(100, 100, 600, 400)
        
        # Set up main layout
        self.label = QLabel("Drag and drop video files here", self)
        self.label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.label.setStyleSheet("font-size: 18px; padding: 20px;")

        self.compress_button_group = QGroupBox("Size calculation", self)
        radio_layout = QVBoxLayout()
        self.radio1 = QRadioButton("Target Percentage", self)
        self.radio2 = QRadioButton("Target Size", self)
        self.radio1.setChecked(True)

        radio_layout.addWidget(self.radio1)
        radio_layout.addWidget(self.radio2)
        self.compress_button_group.setLayout(radio_layout)
        
        self.compression_box_percent = QSpinBox(self)
        self.compression_box_percent.setMinimum(1)
        self.compression_box_percent.setMaximum(100)
        self.compression_box_percent.setSuffix("%")

        self.compression_box_mb = QSpinBox(self)
        self.compression_box_mb.setMinimum(1)
        self.compression_box_mb.setMaximum(2147483647)
        self.compression_box_mb.setSuffix("Mb")

        self.compression_box_mb.hide()

        # Connect radio buttons to the toggle function
        self.radio1.toggled.connect(self.toggle_compression_box)
        self.radio2.toggled.connect(self.toggle_compression_box)

        self.compress_button = QPushButton("Compress", self)
        self.compress_button.clicked.connect(compress_video)

        self.file_picker = QFileDialog(self, caption="Browse")

        layout = QVBoxLayout()
        layout.addWidget(self.label)
        layout.addWidget(self.file_picker)
        layout.addWidget(self.compress_button_group)
        layout.addWidget(self.compression_box_percent)
        layout.addWidget(self.compression_box_mb)
        layout.addWidget(self.compress_button)

        container = QWidget()
        container.setLayout(layout)
        self.setCentralWidget(container)

        # Enable drag and drop
        self.setAcceptDrops(True)

    def toggle_compression_box(self):
        """Toggle visibility of spin boxes based on selected radio button."""
        if self.radio1.isChecked():
            self.compression_box_percent.show()
            self.compression_box_mb.hide()
        else:
            self.compression_box_percent.hide()
            self.compression_box_mb.show()
    
    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event):
        urls = event.mimeData().urls()
        files = [url.toLocalFile() for url in urls]
        self.display_files(files)

    def display_files(self, files):
        self.output_text.clear()  # Clear previous metadata
        for file in files:
            self.output_text.appendPlainText(f"File: {file}")
            metadata = self.read_image_metadata(file)
            for key, value in metadata.items():
                self.output_text.appendPlainText(f"  {key}: {value}")
            self.output_text.appendPlainText("")  # Add space between files

    def read_image_metadata(self, file_path):
        """Extract metadata from an image file."""
        metadata = {}
        try:
            with Image.open(file_path) as img:
                # Basic metadata
                metadata["Format"] = img.format
                metadata["Mode"] = img.mode
                metadata["Size"] = img.size
                metadata["Info"] = img.info

                # DPI (dots per inch)
                dpi = img.info.get('dpi', 'Not available')
                metadata["DPI"] = dpi

                # EXIF Data (if available)
                if hasattr(img, "_getexif"):
                    exif_data = img._getexif()
                    if exif_data:
                        metadata["EXIF"] = {ExifTags.TAGS.get(k, k): v for k, v in exif_data.items()}
                    else:
                        metadata["EXIF"] = "No EXIF data"

                # PNG specific metadata
                if isinstance(img, PngImagePlugin.PngImageFile):
                    metadata["PNG"] = img.info
                    if 'icc_profile' in img.info:
                        metadata["ICC Profile"] = "Present"
                    else:
                        metadata["ICC Profile"] = "Not present"

                # TIFF specific metadata
                if isinstance(img, TiffImagePlugin.TiffImageFile):
                    metadata["TIFF"] = img.tag_v2

                # Other image-specific properties
                if img.format == 'JPEG':
                    if hasattr(img, 'is_jpeg'):
                        metadata["JPEG"] = img.info.get("compression", "Unknown compression")
                metadata["Palette"] = img.getpalette() if img.mode == "P" else "Not a palette-based image"
                
                return metadata
        except Exception as e:
            metadata["Error"] = str(e)
        return metadata


if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = DragDropWidget()
    window.show()
    sys.exit(app.exec())
