#!/usr/bin/env python3

from PyQt6.QtWidgets import *
from PyQt6.QtCore import Qt, QThread, pyqtSignal
from compress import *
import sys
import os
import subprocess

class VideoCompressorThread(QThread):
    progress = pyqtSignal(int)
    completed = pyqtSignal(bool)

    
    def __init__(self, ffmpeg_bin, file_path, target_percentage, codec):
        super().__init__()
        self.ffmpeg_bin = ffmpeg_bin
        self.file_path = file_path
        self.target_percentage = target_percentage
        self.codec = codec


    def run(self):
        """Run the compression in a separate thread and emit progress."""
        try:
            output_path = f"compressed_{os.path.basename(self.file_path)}"
            command = [
                self.ffmpeg_bin,
                "-i", self.file_path,
                "-vcodec", self.codec,
                "-b:v", f"{self.target_percentage}k",
                output_path,
                "-progress", "pipe:1",
                "-nostats"
            ]

            process = subprocess.Popen(command, 
                                       stdout=subprocess.PIPE, 
                                       stderr=subprocess.STDOUT, 
                                       text=True)
            
            # Calculate progress
            for line in process.stdout:
                if "out_time_ms" in line:
                    try:
                        time_ms = int(line.split('=')[1].strip())
                        duration = int(get_video_duration(ffprobe_bin, self.file_path) * 1000)
                        
                        if duration > 0:
                            progress = int((time_ms / duration) * 100)
                            self.progress.emit(progress)
                    
                    except ValueError as e:
                        print(f"Error parsing progress: {e}")
                    
            # process.wait()
            # self.completed.emit(process.returncode == 0)

        except Exception as e:
            self.completed.emit(False)

class DragDropWidget(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Video Utils")
        self.setGeometry(100, 100, 600, 400)
        
        # Main layout
        self.label = QLabel("Drag and drop video files here", self)
        self.label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.label.setStyleSheet("font-size: 18px; padding: 20px;")

        # Radio button group for compression method
        self.compress_button_group = QGroupBox("Size calculation", self)
        radio_layout = QVBoxLayout()
        self.radio1 = QRadioButton("Target Percentage", self)
        self.radio2 = QRadioButton("Target Size", self)
        self.radio1.setChecked(True)
        radio_layout.addWidget(self.radio1)
        radio_layout.addWidget(self.radio2)
        self.compress_button_group.setLayout(radio_layout)
        
        # Spin boxes for target percentage and size
        self.compression_box_percent = QSpinBox(self)
        self.compression_box_percent.setMinimum(1)
        self.compression_box_percent.setMaximum(100)
        self.compression_box_percent.setSuffix("%")

        self.compression_box_mb = QSpinBox(self)
        self.compression_box_mb.setMinimum(1)
        self.compression_box_mb.setMaximum(2147483647)
        self.compression_box_mb.setSuffix("MB")
        self.compression_box_mb.hide()

        # Connect radio buttons to the toggle function
        self.radio1.toggled.connect(self.toggle_compression_box)
        self.radio2.toggled.connect(self.toggle_compression_box)

        # File picker for manual file selection
        self.file_picker_button = QPushButton("Browse File", self)
        self.file_picker_button.clicked.connect(self.open_file_dialog)
        
        # Compress button
        self.compress_button = QPushButton("Compress", self)
        self.compress_button.clicked.connect(self.start_compression)

        # Progress bar
        self.progress_bar = QProgressBar(self)
        self.progress_bar.setValue(0)
        self.progress_bar.setVisible(False)


        # Layout setup
        layout = QVBoxLayout()
        layout.addWidget(self.label)
        layout.addWidget(self.file_picker_button)
        layout.addWidget(self.compress_button_group)
        layout.addWidget(self.compression_box_percent)
        layout.addWidget(self.compression_box_mb)
        layout.addWidget(self.compress_button)
        layout.addWidget(self.progress_bar)

        container = QWidget()
        container.setLayout(layout)
        self.setCentralWidget(container)

        # File to be compressed
        self.file_path = None

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
    

    def open_file_dialog(self):
        """Open a file dialog to select a video file."""
        file_dialog = QFileDialog(self, "Select Video File")
        file_dialog.setFileMode(QFileDialog.FileMode.ExistingFile)
        file_dialog.setNameFilter("Video Files (*.mp4 *.mov *.avi *.mkv)")

        if file_dialog.exec():
            self.file_path = file_dialog.selectedFiles()[0]
            self.label.setText(f"Selected File: {self.file_path}")


    def start_compression(self):
        """Start the video compression process"""
        if not self.file_path:
            QMessageBox.warning(self, "No File Selected", "Please select a video file first!")
            return
        
        if not ffmpeg_bin:
            QMessageBox.critical(self, "FFmpeg Error", "FFmpeg not found in system PATH.")
            return
        
        target_value = None
        if self.radio1.isChecked():
            target_value = self.compression_box_percent.value()
        elif self.radio2.isChecked():
            target_value = self.compression_box_mb.value()

            # Calculate the target percemtage based on file size
            original_size = os.path.getsize(self.file_path) / (1024 * 1024) # Convert to MB
            target_value = (target_value / original_size) * 100

        best_codec = detect_best_codec(ffmpeg_bin)

        # Show progress bar
        self.progress_bar.setValue(0)
        self.progress_bar.setVisible(True)

        # Initialise and start the compression thread
        self.compressor_thread = VideoCompressorThread(ffmpeg_bin, self.file_path, target_value, best_codec)
        self.compressor_thread.progress.connect(self.update_progress)
        self.compressor_thread.completed.connect(self.compression_complete)
        self.compressor_thread.start()
    

    def update_progress(self, value):
        """Update progress bar with the current value."""
        self.progress_bar.setValue(value)


    def compression_complete(self, success):
        """Handle completion of the compression process."""
        self.progress_bar.setVisible(False)
        
        if success:
            QMessageBox.information(self, "Success", "Video compression completed successfully!")
        else:
            QMessageBox.critical(self, "Compression Error", "An error occurred during compression.")


    def dragEnterEvent(self, event):
        """Accept drag-and-drop events for video files."""
        if event.mimeData().hasUrls():
            event.acceptProposedAction()


    def dropEvent(self, event):
        """Handle dropped files."""
        urls = event.mimeData().urls()
        files = [url.toLocalFile() for url in urls]
        if files:
            self.file_path = files[0]
            self.label.setText(f"Selected File: {self.file_path}")


if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = DragDropWidget()
    window.show()
    sys.exit(app.exec())
