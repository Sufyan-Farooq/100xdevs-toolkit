import os
import sys
import re
import time
import subprocess
import requests
import json
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

# Constants
COURSES_TO_DOWNLOAD = ["15", "16"]

# Load token from environment or a local .env file (excluded from Git)
TOKEN = os.environ.get("COHORT_TOKEN")
if not TOKEN:
    env_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if os.path.exists(env_file):
        try:
            with open(env_file, "r", encoding="utf-8") as f:
                for line in f:
                    if line.strip().startswith("COHORT_TOKEN="):
                        TOKEN = line.strip().split("=", 1)[1].strip().strip('"').strip("'")
                        break
        except Exception:
            pass

# Fallback: ask for input if running interactively
if not TOKEN:
    print("Authorization Token not found.")
    print("Please set the 'COHORT_TOKEN' environment variable or create a local '.env' file containing:")
    print("COHORT_TOKEN=Bearer <your_token>")
    print("-" * 50)
    try:
        TOKEN = input("Please enter your Authorization Token manually: ").strip()
    except (KeyboardInterrupt, EOFError):
        sys.exit("\nExiting: Token entry cancelled.")
    except Exception:
        pass
    if not TOKEN:
        raise ValueError("Authentication token is missing. Please set the 'COHORT_TOKEN' environment variable.")

if TOKEN and not TOKEN.startswith("Bearer "):
    TOKEN = "Bearer " + TOKEN

HEADERS = {
    "sec-ch-ua-platform": "Windows",
    "Authorization": TOKEN,
    "Referer": "https://app.100xdevs.com/",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36",
    "Content-Type": "application/json"
}

DOWNLOADS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "downloads")
STATUS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "download_status.json")
JS_DATA_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "course_data.js")
LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "download_log.txt")

status_lock = threading.Lock()
print_lock = threading.Lock()

def safe_print(message):
    """Thread-safe printing to stdout and logging to download_log.txt."""
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
    log_message = f"[{timestamp}] {message}"
    with print_lock:
        print(message)
        sys.stdout.flush()
        try:
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(log_message + "\n")
        except:
            pass

def sanitize_name(name):
    """Sanitize directory or file names for Windows filesystem."""
    sanitized = re.sub(r'[\\/*?:"<>|]', '_', name)
    return sanitized.strip(" .")

def sanitize_folder_path(folder_path):
    """Splits path by / and sanitizes each part, then joins with os.path.join to create nested subfolders."""
    if not folder_path:
        return ""
    parts = folder_path.split("/")
    sanitized_parts = [sanitize_name(p) for p in parts if p.strip()]
    return os.path.join(*sanitized_parts) if sanitized_parts else ""

def requests_get_with_retry(url, headers=None, max_retries=5, backoff_factor=2, stream=False, timeout=15):
    """Wrapper around requests.get with retries for network resilience."""
    for attempt in range(1, max_retries + 1):
        try:
            r = requests.get(url, headers=headers, stream=stream, timeout=timeout)
            if r.status_code in (429, 500, 502, 503, 504):
                sleep_time = backoff_factor ** attempt
                safe_print(f"[Retry] Server error {r.status_code} for {url}. Retrying in {sleep_time}s (Attempt {attempt}/{max_retries})...")
                time.sleep(sleep_time)
                continue
            return r
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
            sleep_time = backoff_factor ** attempt
            safe_print(f"[Retry] Network issue ({type(e).__name__}) for {url}. Retrying in {sleep_time}s (Attempt {attempt}/{max_retries})...")
            time.sleep(sleep_time)
            
    # Attempt one last time to throw the final exception if it keeps failing
    return requests.get(url, headers=headers, stream=stream, timeout=timeout)

def load_status():
    """Loads status database file with thread safety."""
    with status_lock:
        if os.path.exists(STATUS_FILE):
            try:
                with open(STATUS_FILE, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                safe_print(f"Error loading status file: {e}. Starting fresh.")
        return {}

def save_status(status_data):
    """Saves status database file with thread safety."""
    with status_lock:
        try:
            with open(STATUS_FILE, "w", encoding="utf-8") as f:
                json.dump(status_data, f, indent=2)
        except Exception as e:
            safe_print(f"Error saving status file: {e}")

def update_video_status(course_id, video_id, title, folder_path, status, file_path=None, sorting=0):
    """Schedules a safe database status update for a video."""
    db = load_status()
    cid_key = str(course_id)
    vid_key = str(video_id)
    
    if cid_key not in db:
        db[cid_key] = {}
        
    db[cid_key][vid_key] = {
        "title": title,
        "folder_path": folder_path,
        "status": status,
        "file_path": file_path,
        "sorting": sorting,
        "last_updated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    }
    save_status(db)

def get_course_titles():
    """Fetches user courses and returns a map of course_id -> sanitized_title."""
    safe_print("Fetching course list to resolve folder names...")
    url = "https://course-backend.100xdevs.com/courses"
    try:
        response = requests_get_with_retry(url, headers=HEADERS, timeout=15)
        if response.status_code == 200:
            courses = response.json().get("data", [])
            mapping = {}
            for c in courses:
                cid = str(c.get("id"))
                title = c.get("title", f"Course {cid}")
                clean_title = re.sub(r'\s*\|\s*\(Completed\)', '', title)
                mapping[cid] = sanitize_name(clean_title)
            return mapping
        else:
            safe_print(f"Failed to fetch courses. Status: {response.status_code}")
    except Exception as e:
        safe_print(f"Error fetching course list: {e}")
    
    return {"15": "Complete Web Development Cohort", "16": "Complete Devops Cohort"}

def verify_video_integrity(filepath):
    """Validates the downloaded MP4 container using ffprobe."""
    if not os.path.exists(filepath):
        return False
        
    cmd = [
        "ffprobe",
        "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=codec_name",
        "-of", "default=noprint_wrappers=1:nokey=1",
        filepath
    ]
    try:
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if result.returncode == 0 and result.stdout.strip():
            return True
        else:
            safe_print(f"ffprobe validation failed for {os.path.basename(filepath)}. Error: {result.stderr.strip()}")
            return False
    except Exception as e:
        safe_print(f"Error running ffprobe on {os.path.basename(filepath)}: {e}")
        return False

def download_file(url, output_path, video_title):
    """Downloads a file via HTTP range requests with discrete progress prints to handle concurrency."""
    temp_path = output_path + ".tmp"
    resume_header = {}
    mode = "wb"
    initial_bytes = 0
    
    if os.path.exists(temp_path):
        initial_bytes = os.path.getsize(temp_path)
        resume_header = {"Range": f"bytes={initial_bytes}-"}
        mode = "ab"
        safe_print(f"[Download] Resuming {video_title} from {initial_bytes / (1024*1024):.2f} MB...")
    else:
        safe_print(f"[Download] Starting {video_title}...")

    try:
        response = requests_get_with_retry(url, headers={**HEADERS, **resume_header}, stream=True, timeout=30)
        
        if response.status_code == 200 and initial_bytes > 0:
            safe_print(f"[Download] Server does not support resume for {video_title}. Restarting...")
            mode = "wb"
            initial_bytes = 0
        elif response.status_code not in (200, 206):
            safe_print(f"[Download] Failed HTTP status {response.status_code} for {video_title}")
            return False
            
        total_size = int(response.headers.get('content-length', 0)) + initial_bytes
        
        milestones = {25: False, 50: False, 75: False}
        
        with open(temp_path, mode) as f:
            downloaded = initial_bytes
            for chunk in response.iter_content(chunk_size=1024*1024):
                if chunk:
                    f.write(chunk)
                    downloaded += len(chunk)
                    
                    if total_size > 0:
                        pct = (downloaded / total_size) * 100
                        for milestone in list(milestones.keys()):
                            if pct >= milestone and not milestones[milestone]:
                                safe_print(f"[Download] {video_title} is {milestone}% complete ({downloaded / (1024*1024):.1f}/{total_size / (1024*1024):.1f} MB)")
                                milestones[milestone] = True
            
        if os.path.exists(output_path):
            os.remove(output_path)
        os.rename(temp_path, output_path)
        safe_print(f"[Download] Finished writing {video_title}.")
        return True
    except Exception as e:
        safe_print(f"[Download] Error downloading {video_title}: {e}")
        return False

def download_via_ffmpeg(m3u8_url, output_path, video_title):
    """Fallback method to download HLS streams via ffmpeg copy."""
    safe_print(f"[ffmpeg] Downloading HLS stream for {video_title}...")
    cmd = [
        "ffmpeg",
        "-y",
        "-headers", f"Referer: https://app.100xdevs.com/\r\nUser-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36\r\n",
        "-i", m3u8_url,
        "-c", "copy",
        output_path
    ]
    try:
        process = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if process.returncode == 0:
            safe_print(f"[ffmpeg] Download success: {video_title}")
            return True
        else:
            safe_print(f"[ffmpeg] Failed code {process.returncode} for {video_title}")
            return False
    except Exception as e:
        safe_print(f"[ffmpeg] Error invoking ffmpeg for {video_title}: {e}")
        return False

def check_and_create_hardlink(course_id, video_title, folder_path, target_path):
    """Checks if the same video exists and is validated in another course. If so, creates a hardlink."""
    db = load_status()
    for cid_key, vids in db.items():
        if str(cid_key) == str(course_id):
            continue  # Skip checking in the current course folder itself
            
        for vid_key, v_info in vids.items():
            # Match by identical title and relative folder path
            if v_info.get("title") == video_title and v_info.get("folder_path") == folder_path:
                source_file = v_info.get("file_path")
                if source_file and os.path.exists(source_file) and v_info.get("status") == "validated":
                    # Double check integrity of source file
                    if verify_video_integrity(source_file):
                        try:
                            # Create a hardlink
                            os.makedirs(os.path.dirname(target_path), exist_ok=True)
                            if os.path.exists(target_path):
                                os.remove(target_path)
                            os.link(source_file, target_path)
                            safe_print(f"[Deduplication] SUCCESS: Hardlinked {video_title} from {os.path.basename(source_file)} (Saved download size!)")
                            return True
                        except Exception as e:
                            safe_print(f"[Deduplication] Failed to link: {e}")
    return False

def process_video_task(course_id, video_id, video_title, target_folder, folder_path, sorting):
    """Worker task that handles deduplication lookup, metadata lookup, download, and integrity validation."""
    safe_title = sanitize_name(video_title)
    output_filename = f"{safe_title}.mp4"
    output_path = os.path.join(target_folder, output_filename)
    
    update_video_status(course_id, video_id, video_title, folder_path, "downloading", output_path, sorting)
    
    # Try hardlink deduplication first
    if check_and_create_hardlink(course_id, video_title, folder_path, output_path):
        update_video_status(course_id, video_id, video_title, folder_path, "validated", output_path, sorting)
        export_course_data_js()
        return True
        
    url = f"https://course-backend.100xdevs.com/courses/{course_id}/video/{video_id}"
    
    try:
        response = requests_get_with_retry(url, headers=HEADERS, timeout=15)
        if response.status_code != 200:
            safe_print(f"[Metadata] Failed fetch for {video_title} (HTTP {response.status_code})")
            update_video_status(course_id, video_id, video_title, folder_path, "failed", output_path, sorting)
            return False
            
        video_data = response.json().get("data", {})
        if not video_data:
            safe_print(f"[Metadata] Empty video data for {video_title}")
            update_video_status(course_id, video_id, video_title, folder_path, "failed", output_path, sorting)
            return False
            
        qualities = video_data.get("qualities", [])
        download_url = None
        
        if qualities:
            def quality_val(q):
                label = q.get("label", "0p").lower()
                num = re.findall(r'\d+', label)
                return int(num[0]) if num else 0
                
            sorted_qualities = sorted(qualities, key=quality_val, reverse=True)
            best_quality = sorted_qualities[0]
            download_url = best_quality.get("url")
        else:
            download_url = video_data.get("mp4Url")
            
        success = False
        if download_url:
            success = download_file(download_url, output_path, video_title)
            
        if not success:
            hls_url = video_data.get("hlsUrl")
            if hls_url:
                success = download_via_ffmpeg(hls_url, output_path, video_title)
                
        if success:
            safe_print(f"[Validation] Checking integrity for: {video_title}...")
            if verify_video_integrity(output_path):
                safe_print(f"[Validation] SUCCESS: {video_title} validated.")
                update_video_status(course_id, video_id, video_title, folder_path, "validated", output_path, sorting)
                export_course_data_js()
                return True
            else:
                safe_print(f"[Validation] FAIL: {video_title} corrupt. Deleting file...")
                if os.path.exists(output_path):
                    os.remove(output_path)
                update_video_status(course_id, video_id, video_title, folder_path, "failed", output_path, sorting)
                return False
        else:
            safe_print(f"[Download] FAILED: {video_title}")
            update_video_status(course_id, video_id, video_title, folder_path, "failed", output_path, sorting)
            return False
    except Exception as e:
        safe_print(f"[Task Error] Failed processing video {video_title}: {e}")
        update_video_status(course_id, video_id, video_title, folder_path, "failed", output_path, sorting)
        return False

def crawl_course_metadata(course_id, parent_id=None, folder_path=""):
    """Crawls course API and collects list of all video structures recursively."""
    url = f"https://course-backend.100xdevs.com/courses/{course_id}/content"
    if parent_id:
        url += f"?parentId={parent_id}"
        
    collected_videos = []
    
    try:
        response = requests_get_with_retry(url, headers=HEADERS, timeout=15)
        time.sleep(0.3)
        
        if response.status_code == 200:
            items = response.json().get("data", [])
            for item in items:
                m_type = item.get("materialType")
                title = item.get("title", "")
                item_id = item.get("id")
                sorting = item.get("sorting", 0)
                
                if m_type == "FOLDER":
                    new_path = f"{folder_path}/{title}" if folder_path else title
                    collected_videos.extend(crawl_course_metadata(course_id, item_id, new_path))
                elif m_type == "VIDEO":
                    collected_videos.append({
                        "course_id": course_id,
                        "video_id": item_id,
                        "title": title,
                        "folder_path": folder_path,
                        "sorting": sorting
                    })
        else:
            safe_print(f"Error crawling course {course_id} content. HTTP {response.status_code}")
    except Exception as e:
        safe_print(f"Exception crawling course {course_id}, parent {parent_id}: {e}")
        
    return collected_videos

def migrate_old_paths(course_titles):
    """Migrates files from legacy folders (both root and course subfolders) to proper nested directories, removing duplicates."""
    safe_print("Running directory structure migration check...")
    db = load_status()
    db_changed = False
    
    for cid, vids in db.items():
        course_name = course_titles.get(cid, f"Course_{cid}")
        for vid, v_info in vids.items():
            folder_path = v_info.get("folder_path", "")
            title = v_info.get("title", "")
            
            # Legacy files can either have folder_path or be root level (folder_path = "")
            old_folder_name = sanitize_name(folder_path) if folder_path else ""
            new_folder_rel = sanitize_folder_path(folder_path)
            
            safe_title = sanitize_name(title)
            filename = f"{safe_title}.mp4"
            
            # The destination path (correct nested path)
            new_dir = os.path.join(DOWNLOADS_DIR, course_name, new_folder_rel)
            new_file = os.path.join(new_dir, filename)
            new_tmp = new_file + ".tmp"
            
            # Legacy candidate files to scan
            candidates = []
            if folder_path:
                # Candidate A: downloads/course_name/Old_Folder_Name/filename (from intermediate flat fix)
                candidates.append(os.path.join(DOWNLOADS_DIR, course_name, old_folder_name, filename))
                # Candidate B: downloads/Old_Folder_Name/filename (from legacy root run)
                candidates.append(os.path.join(DOWNLOADS_DIR, old_folder_name, filename))
            else:
                # Root level candidate: downloads/filename (from legacy root run)
                candidates.append(os.path.join(DOWNLOADS_DIR, filename))
                
            for old_file in candidates:
                old_tmp = old_file + ".tmp"
                
                # Verify that old and new paths are different before attempting a migration
                if old_file != new_file:
                    # Case A: Old file exists and correct destination already exists and is validated
                    if os.path.exists(old_file) and os.path.exists(new_file) and v_info.get("status") == "validated":
                        try:
                            # Safely delete duplicate to clean up disk space
                            os.remove(old_file)
                            safe_print(f"[Migration] Deleted duplicate file: {os.path.basename(old_file)}")
                        except Exception as e:
                            pass
                            
                    # Case B: Old file exists but destination does not
                    elif os.path.exists(old_file) and not os.path.exists(new_file):
                        os.makedirs(new_dir, exist_ok=True)
                        try:
                            os.rename(old_file, new_file)
                            safe_print(f"[Migration] Moved video: {filename} -> {new_folder_rel}")
                            v_info["file_path"] = new_file
                            db_changed = True
                        except Exception as e:
                            safe_print(f"[Migration] Error moving {old_file}: {e}")
                            
                    # Case C: Old temp file exists but destination temp does not
                    if os.path.exists(old_tmp) and not os.path.exists(new_tmp) and not os.path.exists(new_file):
                        os.makedirs(new_dir, exist_ok=True)
                        try:
                            os.rename(old_tmp, new_tmp)
                            safe_print(f"[Migration] Moved temp video: {filename}.tmp -> {new_folder_rel}")
                            v_info["file_path"] = new_file
                            db_changed = True
                        except Exception as e:
                            safe_print(f"[Migration] Error moving {old_tmp}: {e}")
                            
            if v_info.get("status") == "validated" and v_info.get("file_path") != new_file:
                if os.path.exists(new_file):
                    v_info["file_path"] = new_file
                    db_changed = True

    if db_changed:
        save_status(db)
        
    # Clean up empty legacy folders from the rootdownloads/ directory
    for item in os.listdir(DOWNLOADS_DIR):
        item_path = os.path.join(DOWNLOADS_DIR, item)
        if os.path.isdir(item_path) and item not in ["Complete Devops Cohort", "Complete Web Development Cohort"]:
            # Check if empty, recursively removing nested subfolders
            try:
                # Recursively delete directories if they are empty
                for root, dirs, files in os.walk(item_path, topdown=False):
                    for d in dirs:
                        d_path = os.path.join(root, d)
                        if len(os.listdir(d_path)) == 0:
                            os.rmdir(d_path)
                if len(os.listdir(item_path)) == 0:
                    os.rmdir(item_path)
                    safe_print(f"[Cleanup] Removed empty legacy folder: {item}")
            except Exception as e:
                pass

def export_course_data_js():
    """Generates course_data.js exporting the nested folder hierarchy of all validated videos for the HTML player."""
    db = load_status()
    course_titles = {"15": "Complete Web Development Cohort", "16": "Complete Devops Cohort"}
    courses_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "courses")
    
    courses_tree = []
    
    for cid in COURSES_TO_DOWNLOAD:
        course_name = course_titles.get(cid, f"Course_{cid}")
        vids_db = db.get(cid, {})
        
        # Check if canonical content_tree.json exists in courses/
        tree_path = None
        if os.path.exists(courses_dir):
            for folder in os.listdir(courses_dir):
                if folder.startswith(f"{cid}_") or folder == str(cid):
                    cand = os.path.join(courses_dir, folder, "content_tree.json")
                    if os.path.exists(cand):
                        tree_path = cand
                        break
                        
        c_dl_dir = os.path.join(DOWNLOADS_DIR, course_name)
        disk_files = {}
        if os.path.exists(c_dl_dir):
            for root, dirs, files in os.walk(c_dl_dir):
                for f in files:
                    if f.endswith(('.mp4', '.mkv', '.webm')):
                        full_p = os.path.join(root, f)
                        norm = full_p.replace("\\", "/")
                        idx = norm.find("downloads/")
                        rel_p = norm[idx:] if idx != -1 else os.path.basename(full_p)
                        clean_f = os.path.splitext(f)[0].lower()
                        disk_files[clean_f] = rel_p

        def resolve_video_path(item_id, item_title):
            # 1. Check in DB by id
            v_info = vids_db.get(str(item_id))
            if v_info and v_info.get("status") == "validated" and v_info.get("file_path"):
                fp = v_info["file_path"]
                if os.path.exists(fp):
                    norm = fp.replace("\\", "/")
                    idx = norm.find("downloads/")
                    return norm[idx:] if idx != -1 else os.path.basename(fp)
            # 2. Check in DB by title
            for vid, v in vids_db.items():
                if v.get("title") == item_title and v.get("status") == "validated" and v.get("file_path"):
                    fp = v["file_path"]
                    if os.path.exists(fp):
                        norm = fp.replace("\\", "/")
                        idx = norm.find("downloads/")
                        return norm[idx:] if idx != -1 else os.path.basename(fp)
            # 3. Check disk_files index
            clean_t = sanitize_name(item_title).lower()
            if clean_t in disk_files:
                return disk_files[clean_t]
            for k, v in disk_files.items():
                if clean_t in k or k in clean_t:
                    return v
            alnum_t = ''.join(c for c in item_title if c.isalnum()).lower()
            for k, v in disk_files.items():
                if ''.join(c for c in k if c.isalnum()) == alnum_t:
                    return v
            return None

        if tree_path:
            try:
                with open(tree_path, 'r', encoding='utf-8') as f:
                    syllabus = json.load(f)
                    
                def process_nodes(nodes):
                    result = []
                    for n in nodes:
                        t = n.get('type')
                        title = n.get('title')
                        sorting = n.get('sorting', 0)
                        
                        if t == 'FOLDER':
                            sub_children = process_nodes(n.get('children', []))
                            if sub_children:
                                result.append({
                                    "title": title,
                                    "type": "folder",
                                    "children": sub_children
                                })
                        elif t == 'VIDEO':
                            rel_p = resolve_video_path(n.get('id'), title)
                            if rel_p:
                                result.append({
                                    "title": title,
                                    "type": "video",
                                    "file_path": rel_p,
                                    "sorting": sorting
                                })
                    return result

                course_root = {
                    "title": course_name,
                    "type": "course",
                    "children": process_nodes(syllabus.get('tree', []))
                }
                courses_tree.append(course_root)
                continue
            except Exception as e:
                safe_print(f"[JS Export] Error processing syllabus tree for course {cid}: {e}. Falling back to DB.")

        # Fallback to reconstructing from DB with strict file_path deduplication
        validated_vids = []
        seen_files = set()
        for vid, v_info in vids_db.items():
            if v_info.get("status") == "validated":
                fp = v_info.get("file_path", "")
                norm_fp = fp.replace("\\", "/").lower() if fp else ""
                if norm_fp and norm_fp in seen_files:
                    continue
                if norm_fp:
                    seen_files.add(norm_fp)
                validated_vids.append({
                    "id": vid,
                    "title": v_info.get("title", ""),
                    "folder_path": v_info.get("folder_path", ""),
                    "file_path": v_info.get("file_path", ""),
                    "sorting": v_info.get("sorting", 0)
                })
                
        if not validated_vids:
            continue
            
        course_root = {
            "title": course_name,
            "type": "course",
            "children": []
        }
        
        def get_folder_node(root_list, path_str):
            if not path_str:
                return root_list
            parts = path_str.split("/")
            current_list = root_list
            for part in parts:
                part_clean = part.strip()
                if not part_clean:
                    continue
                found_folder = None
                for node in current_list:
                    if node.get("type") == "folder" and node.get("title") == part_clean:
                        found_folder = node
                        break
                if not found_folder:
                    found_folder = {
                        "title": part_clean,
                        "type": "folder",
                        "children": []
                    }
                    current_list.append(found_folder)
                current_list = found_folder["children"]
            return current_list
            
        for v in validated_vids:
            target_list = get_folder_node(course_root["children"], v["folder_path"])
            rel_path = ""
            if v["file_path"]:
                norm_path = v["file_path"].replace("\\", "/")
                idx = norm_path.find("downloads/")
                if idx != -1:
                    rel_path = norm_path[idx:]
                else:
                    rel_path = os.path.basename(v["file_path"])
            target_list.append({
                "title": v["title"],
                "type": "video",
                "file_path": rel_path,
                "sorting": v["sorting"]
            })
            
        def sort_children(node_list):
            def sort_key(node):
                is_video = 1 if node.get("type") == "video" else 0
                sorting_val = node.get("sorting", 0)
                title = node.get("title", "").lower()
                return (is_video, sorting_val, title)
            node_list.sort(key=sort_key)
            for node in node_list:
                if node.get("type") == "folder":
                    sort_children(node["children"])
                    
        sort_children(course_root["children"])
        courses_tree.append(course_root)
        
    try:
        # Write to course_data.js
        with open(JS_DATA_FILE, "w", encoding="utf-8") as f:
            f.write(f"const courseData = {json.dumps(courses_tree, indent=2)};\n")
        # safe_print("[JS Export] Successfully wrote course_data.js.")
    except Exception as e:
        safe_print(f"Error exporting course_data.js: {e}")

def run_downloader(single_test_id=None):
    course_titles = get_course_titles()
    
    # 1. Catalog course files
    all_videos = []
    for cid in COURSES_TO_DOWNLOAD:
        course_name = course_titles.get(cid, f"Course_{cid}")
        safe_print(f"Cataloging files for {course_name}...")
        videos = crawl_course_metadata(cid)
        for v in videos:
            v["course_name"] = course_name
        all_videos.extend(videos)
        
    safe_print(f"Crawl finished. Found total {len(all_videos)} videos across all courses.")
    
    # 2. Merge catalog into status database
    db = load_status()
    for v in all_videos:
        cid = str(v["course_id"])
        vid = str(v["video_id"])
        
        if cid not in db:
            db[cid] = {}
        if vid not in db[cid]:
            db[cid][vid] = {
                "title": v["title"],
                "folder_path": v["folder_path"],
                "status": "pending",
                "file_path": None,
                "sorting": v["sorting"],
                "last_updated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            }
    save_status(db)
    
    # Run structural directory migration
    migrate_old_paths(course_titles)
    # Generate initial course_data.js from existing validated items
    export_course_data_js()
    
    # If single test id is requested
    if single_test_id:
        target_video = None
        for v in all_videos:
            if str(v["video_id"]) == str(single_test_id):
                target_video = v
                break
        if target_video:
            safe_print(f"Running single test download for video: {target_video['title']}")
            dest_dir = os.path.join(DOWNLOADS_DIR, target_video["course_name"], sanitize_folder_path(target_video["folder_path"]))
            os.makedirs(dest_dir, exist_ok=True)
            process_video_task(
                target_video["course_id"], 
                target_video["video_id"], 
                target_video["title"], 
                dest_dir, 
                target_video["folder_path"],
                target_video["sorting"]
            )
        else:
            safe_print(f"Test video ID {single_test_id} not found in crawl results.")
        return

    # 3. Perform startup integrity verification for files on disk
    safe_print("Verifying existing downloads integrity...")
    db = load_status()
    db_changed = False
    
    # Pre-map all videos by expected output path to check for physical downloads on disk
    video_paths_to_verify = {}
    for v in all_videos:
        cid = str(v["course_id"])
        vid = str(v["video_id"])
        
        safe_title = sanitize_name(v["title"])
        dest_dir = os.path.join(DOWNLOADS_DIR, v["course_name"], sanitize_folder_path(v["folder_path"]))
        output_path = os.path.normpath(os.path.join(dest_dir, f"{safe_title}.mp4"))
        
        video_paths_to_verify[output_path] = (cid, vid, v["title"], v["folder_path"], v["sorting"])

    # Verify physical file presence on disk
    for path, (cid, vid, title, folder_path, sorting) in video_paths_to_verify.items():
        if os.path.exists(path):
            status = db.get(cid, {}).get(vid, {}).get("status")
            if status != "validated":
                safe_print(f"Validating unconfirmed file on disk: {title}...")
                if verify_video_integrity(path):
                    safe_print(f"SUCCESS: {title} verified on disk.")
                    db[cid][vid] = {
                        "title": title,
                        "folder_path": folder_path,
                        "status": "validated",
                        "file_path": path,
                        "sorting": sorting,
                        "last_updated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                    }
                    db_changed = True
                else:
                    safe_print(f"FAIL: {title} corrupt on disk. Deleting file...")
                    try:
                        os.remove(path)
                    except:
                        pass
                    db[cid][vid]["status"] = "pending"
                    db_changed = True
            else:
                # If marked validated, double check path matches
                if db.get(cid, {}).get(vid, {}).get("file_path") != path:
                    db[cid][vid]["file_path"] = path
                    db_changed = True
        else:
            status = db.get(cid, {}).get(vid, {}).get("status")
            if status == "validated":
                safe_print(f"Validated file missing on disk: {title}. Re-scheduling.")
                db[cid][vid]["status"] = "pending"
                db_changed = True

    if db_changed:
        save_status(db)
        
    # Check if we can hardlink validated files from other courses for pending videos
    db = load_status()
    db_changed = False
    for v in all_videos:
        cid = str(v["course_id"])
        vid = str(v["video_id"])
        status = db.get(cid, {}).get(vid, {}).get("status", "pending")
        
        if status != "validated":
            safe_title = sanitize_name(v["title"])
            dest_dir = os.path.join(DOWNLOADS_DIR, v["course_name"], sanitize_folder_path(v["folder_path"]))
            output_path = os.path.normpath(os.path.join(dest_dir, f"{safe_title}.mp4"))
            
            if check_and_create_hardlink(v["course_id"], v["title"], v["folder_path"], output_path):
                db[cid][vid] = {
                    "title": v["title"],
                    "folder_path": v["folder_path"],
                    "status": "validated",
                    "file_path": output_path,
                    "sorting": v["sorting"],
                    "last_updated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                }
                db_changed = True

    if db_changed:
        save_status(db)
        
    export_course_data_js()

    # 4. Filter remaining videos to be downloaded, preventing duplicate destinations
    db = load_status()
    download_queue = []
    seen_output_paths = set()
    
    for v in all_videos:
        cid = str(v["course_id"])
        vid = str(v["video_id"])
        status = db.get(cid, {}).get(vid, {}).get("status", "pending")
        
        if status != "validated":
            safe_title = sanitize_name(v["title"])
            dest_dir = os.path.normpath(os.path.join(DOWNLOADS_DIR, v["course_name"], sanitize_folder_path(v["folder_path"])))
            output_path = os.path.normpath(os.path.join(dest_dir, f"{safe_title}.mp4"))
            
            if output_path not in seen_output_paths:
                seen_output_paths.add(output_path)
                download_queue.append(v)
            
    if not download_queue:
        safe_print("All videos are fully downloaded and validated!")
        return
        
    # 5. Prioritize the download queue
    def get_priority(v):
        path = v["folder_path"]
        if "DevOps Cohort Videos" in path:
            return 0
        elif "Web Development" in path or "Web Dev" in path:
            return 1
        elif "Extra Content" in path:
            return 2
        else:
            return 3
            
    download_queue.sort(key=get_priority)
    
    safe_print(f"\n--- Queue Prioritization Complete ---")
    safe_print(f"Total videos to download: {len(download_queue)}")
    
    # 6. Execute downloads concurrently (max workers = 3)
    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = {}
        for v in download_queue:
            dest_dir = os.path.join(DOWNLOADS_DIR, v["course_name"], sanitize_folder_path(v["folder_path"]))
            os.makedirs(dest_dir, exist_ok=True)
            
            future = executor.submit(
                process_video_task,
                v["course_id"],
                v["video_id"],
                v["title"],
                dest_dir,
                v["folder_path"],
                v["sorting"]
            )
            futures[future] = v["title"]
            
        for future in as_completed(futures):
            title = futures[future]
            try:
                result = future.result()
            except Exception as e:
                safe_print(f"Exception occurred in worker thread for {title}: {e}")

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--single-test":
        run_downloader(single_test_id="5647")
    else:
        run_downloader()
