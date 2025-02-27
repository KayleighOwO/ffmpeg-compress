import shutil
import subprocess
import sys
import os

# Check for ffmpeg or ffprobe in the system's PATH or specific directories
def get_ffmpeg_and_ffprobe():
    ffmpeg_in_path = shutil.which("ffmpeg")
    ffprobe_in_path = shutil.which("ffprobe")

    if ffmpeg_in_path:
        print(f"FFmpeg found in system PATH: {ffmpeg_in_path}")
        ffmpeg_bin = ffmpeg_in_path
    else:
        ffmpeg_bin = None
        print("FFmpeg not found in system PATH.")
        
    if ffprobe_in_path:
        print(f"FFprobe found in system PATH: {ffprobe_in_path}")
        ffprobe_bin = ffprobe_in_path
    elif ffmpeg_bin:  # If ffmpeg is found but not ffprobe, try the same directory
        ffprobe_bin = os.path.join(os.path.dirname(ffmpeg_bin), "ffprobe")
        if os.path.exists(ffprobe_bin):
            print(f"FFprobe found in the same directory as FFmpeg: {ffprobe_bin}")
        else:
            ffprobe_bin = None
            print("FFprobe not found in the same directory as FFmpeg.")
    else:
        ffprobe_bin = None
        print("FFprobe not found in system PATH and FFmpeg directory.")

    # Return both binaries (ffmpeg and ffprobe)
    return ffmpeg_bin, ffprobe_bin

ffmpeg_bin, ffprobe_bin = get_ffmpeg_and_ffprobe()

# Detect the best codec by getting a list of supported codecs
def detect_best_codec(ffmpeg_path):
    result = subprocess.run(
        [ffmpeg_path, "-hide_banner", "-hwaccels"],
        capture_output=True,
        text=True
    )

    hardware_accels = result.stdout.lower()

    if "cuda" in hardware_accels or "nvenc" in hardware_accels:
        return "h264_nvenc"         # NVIDIA GPU
    elif "qsv" in hardware_accels:
        return "h264_qsv"           # Intel Quick Sync Video
    elif "amf" in hardware_accels:
        return "h264_amf"           # AMD GPU
    else:
        return "libx264"            # Software fallback

# Get video duration
def get_video_duration(ffprobe_bin, file_path):
    # Print the exact command being run
    print(f"Running ffprobe command: {ffprobe_bin} -v error -show_entries format=duration -of default=noprint_wrappers=1:nokey=1 \"{file_path}\"")
    
    duration = subprocess.run(
        [ffprobe_bin, 
         "-v", "error", 
         "-select_streams", "v:0", 
         "-show_entries", "format=duration", 
         "-of", "default=noprint_wrappers=1:nokey=1", 
         file_path],
        
        capture_output=True,
        text=True
    )

    return duration

# Compress video using the previously gotten codec
def compress_video(ffmpeg_bin, file_path, target_percentage, codec):
    filename = os.path.basename(file_path)
    file_dir = os.path.dirname(file_path)
    original_size = os.path.getsize(file_path)
    target_size = int(original_size * (target_percentage / 100))

    # Get video duration
    duration = get_video_duration(ffprobe_bin, file_path)

    # Handle case where ffprobe fails to fetch duration
    if duration.returncode != 0:
        raise RuntimeError(f"Error running ffprobe: {duration.stderr}")
    
    duration = duration.stdout.strip()
    if not duration:
        raise RuntimeError("Failed to get video duration. Output from ffprobe is empty.")
    
    # Now we can safely convert it to a float
    try:
        duration = float(duration)
    except ValueError:
        raise RuntimeError(f"Invalid duration value returned: '{duration}'")

    target_bitrate = (target_size * 8) / duration

    output_filename = f"compressed_{filename}"
    output_path = os.path.join(file_dir, output_filename)

    command = [
        ffmpeg_bin, 
        "-i", file_path, 
        "-vcodec", codec,
        "-b:v", f"{target_bitrate}", 
        "-maxrate:v", f"{target_bitrate * 1.5}",
        "-bufsize:v", f"{target_bitrate * 2}", 
        output_path
    ]

    subprocess.run(command)
    print(f"Compressed video saved as {output_path}")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: python compress.py <file_path> <target_percentage>")
        sys.exit(1)

    input_file = sys.argv[1]
    try:
        target_percentage = float(sys.argv[2])
    except ValueError:
        print("Please provide a valid number for target percentage.")
        sys.exit(1)

    
    best_codec = detect_best_codec(ffmpeg_bin)
    compress_video(ffmpeg_bin, input_file, target_percentage, best_codec)
