import os
import sys
import argparse
import time

# Add pip-installed NVIDIA DLLs to Windows DLL search path on startup
if os.name == 'nt':
    nvidia_paths = []
    for path in sys.path:
        if "site-packages" in path:
            cublas_bin = os.path.join(path, "nvidia", "cublas", "bin")
            cudnn_bin = os.path.join(path, "nvidia", "cudnn", "bin")
            nvrtc_bin = os.path.join(path, "nvidia", "cuda_nvrtc", "bin")
            runtime_bin = os.path.join(path, "nvidia", "cuda_runtime", "bin")
            for dll_dir in [cublas_bin, cudnn_bin, nvrtc_bin, runtime_bin]:
                if os.path.exists(dll_dir):
                    nvidia_paths.append(dll_dir)
                    try:
                        os.add_dll_directory(dll_dir)
                    except Exception:
                        pass
    if nvidia_paths:
        os.environ["PATH"] = ";".join(nvidia_paths) + ";" + os.environ["PATH"]

from faster_whisper import WhisperModel

def format_time(seconds):
    """Formats time in seconds into WEBVTT timestamp format (HH:MM:SS.mmm)."""
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    milliseconds = int((seconds - int(seconds)) * 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}.{milliseconds:03d}"

def transcribe_video(model, video_path, vtt_path):
    print(f"\nTranscribing: {os.path.basename(video_path)}")
    print(f" -> Subtitle Output: {os.path.basename(vtt_path)}")
    
    start_time = time.time()
    
    # Run Whisper transcription
    # We use beam_size=5 for a good speed/accuracy trade-off
    segments, info = model.transcribe(video_path, beam_size=5, language="en")
    
    print(f" -> Detected language: '{info.language}' with probability {info.language_probability:.2f}")
    
    # Write directly to VTT format
    with open(vtt_path, "w", encoding="utf-8") as f:
        f.write("WEBVTT\n\n")
        
        for segment in segments:
            start_str = format_time(segment.start)
            end_str = format_time(segment.end)
            text = segment.text.strip()
            
            f.write(f"{start_str} --> {end_str}\n")
            f.write(f"{text}\n\n")
            
            # Print periodic console updates
            print(f"[{start_str} -> {end_str}]: {text}")
            
    elapsed = time.time() - start_time
    print(f" -> Finished transcribing in {elapsed:.1f} seconds.")

def main():
    parser = argparse.ArgumentParser(description="Auto-generate local subtitles for course videos using Whisper.")
    parser.add_argument("directory", help="Directory path to scan for videos.")
    parser.add_argument("--model", default="small", help="Whisper model scale (tiny, base, small, medium, large-v3).")
    args = parser.parse_args()
    
    if not os.path.exists(args.directory):
        print(f"Error: Directory '{args.directory}' does not exist.")
        sys.exit(1)
        
    # Detect if CUDA (Nvidia GPU) is available and fully functional
    device = "cuda"
    compute_type = "float16"
    
    print("Checking hardware support...")
    try:
        import numpy as np
        test_model = WhisperModel("tiny", device="cuda", compute_type="float16")
        # Transcribe a 0.1-second silent array to force-load CUDA/cuBLAS DLLs and test execution
        dummy_audio = np.zeros(1600, dtype=np.float32)
        segments, _ = test_model.transcribe(dummy_audio)
        list(segments) # Force generator evaluation to trigger DLL loading
        del test_model
        print(" -> Nvidia CUDA GPU acceleration detected and verified! Using GPU.")
    except Exception as e:
        print(f" -> GPU acceleration test failed or DLLs missing ({e}). Falling back to CPU.")
        device = "cpu"
        compute_type = "int8" # Fastest performance on CPU
        
    print(f"Initializing Whisper model '{args.model}' on {device.upper()} ({compute_type})...")
    model = WhisperModel(args.model, device=device, compute_type=compute_type)
    print("Whisper model loaded successfully.")
    
    video_files = []
    for root, dirs, files in os.walk(args.directory):
        for file in files:
            if file.lower().endswith(".mp4") and not file.endswith(".tmp"):
                video_files.append(os.path.join(root, file))
                
    if not video_files:
        print(f"No video (.mp4) files found in '{args.directory}'")
        return
        
    print(f"Found {len(video_files)} video files to process.")
    
    success_count = 0
    for idx, video_path in enumerate(video_files, 1):
        vtt_path = os.path.splitext(video_path)[0] + ".vtt"
        
        # Skip if subtitle already exists
        if os.path.exists(vtt_path):
            print(f"\n[{idx}/{len(video_files)}] Subtitle already exists for: {os.path.basename(video_path)}. Skipping.")
            continue
            
        print(f"\n[{idx}/{len(video_files)}] Processing file...")
        try:
            transcribe_video(model, video_path, vtt_path)
            success_count += 1
        except Exception as e:
            print(f"Error transcribing {os.path.basename(video_path)}: {e}")
            
    print(f"\nAll done! Successfully generated subtitles for {success_count} videos.")

if __name__ == "__main__":
    main()
