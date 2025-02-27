import subprocess

# Get video duration
def get_video_duration(ffmpeg_path, file_path):
    # Print the exact command being run
    print(f"Running ffprobe command: {ffmpeg_path} -v error -show_entries format=duration -of default=noprint_wrappers=1:nokey=1 \"{file_path}\"")
    
    probe = subprocess.run(
        [ffmpeg_path, 
         "-v", "error", 
         "-show_entries", "format=duration", 
         "-of", "default=noprint_wrappers=1:nokey=1", 
         file_path],
        
        capture_output=True,
        text=True
    )

    # Handle case where ffprobe fails to fetch duration
    if probe.returncode != 0:
        raise RuntimeError(f"Error running ffprobe: {probe.stderr}")
    
    duration = probe.stdout.strip()
    if not duration:
        raise RuntimeError("Failed to get video duration. Output from ffprobe is empty.")
    
    # Now we can safely convert it to a float
    try:
        duration = float(duration)
    except ValueError:
        raise RuntimeError(f"Invalid duration value returned: '{duration}'")

    return duration