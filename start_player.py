import http.server
import socketserver
import webbrowser
import threading
import time
import sys
import os
import mimetypes

import json

# Ensure explicit MIME types for video streaming and subtitles
mimetypes.add_type("text/vtt", ".vtt")
mimetypes.add_type("video/mp4", ".mp4")
mimetypes.add_type("video/webm", ".webm")
mimetypes.add_type("application/javascript", ".js")
mimetypes.add_type("text/html", ".html")
mimetypes.add_type("text/css", ".css")
mimetypes.add_type("application/json", ".json")

PORT = 8000
DIRECTORY = "."
USER_DATA_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "user_data.json")
user_data_lock = threading.Lock()

class ThreadedHTTPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address):
        # Suppress connection disconnect errors when browser aborts socket during video seeking
        exc_type = sys.exc_info()[0]
        if exc_type in (ConnectionResetError, ConnectionAbortedError, BrokenPipeError):
            return
        super().handle_error(request, client_address)

class MyHTTPRequestHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, format, *args):
        # Prevent spamming console with standard requests log
        pass

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        path_clean = self.path.split('?')[0]
        if path_clean == "/api/data":
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
            self.end_headers()

            with user_data_lock:
                if os.path.exists(USER_DATA_FILE):
                    try:
                        with open(USER_DATA_FILE, "rb") as f:
                            self.wfile.write(f.read())
                        return
                    except Exception:
                        pass
                default_data = {
                    "history": {},
                    "notes": {},
                    "bookmarks": {},
                    "streak": None,
                    "lastPlayed": None,
                    "settings": {}
                }
                self.wfile.write(json.dumps(default_data, indent=2).encode("utf-8"))
            return

        super().do_GET()

    def do_POST(self):
        path_clean = self.path.split('?')[0]
        if path_clean == "/api/data":
            try:
                content_len = int(self.headers.get('Content-Length', 0))
                post_body = self.rfile.read(content_len)
                payload = json.loads(post_body.decode('utf-8'))

                with user_data_lock:
                    tmp_file = USER_DATA_FILE + ".tmp"
                    with open(tmp_file, "w", encoding="utf-8") as f:
                        json.dump(payload, f, indent=2, ensure_ascii=False)
                    if os.path.exists(USER_DATA_FILE):
                        os.replace(tmp_file, USER_DATA_FILE)
                    else:
                        os.rename(tmp_file, USER_DATA_FILE)

                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                response = json.dumps({"status": "saved", "timestamp": time.time()})
                self.wfile.write(response.encode("utf-8"))
                return
            except Exception as e:
                self.send_response(500)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                response = json.dumps({"error": str(e)})
                self.wfile.write(response.encode("utf-8"))
                return

        self.send_error(404, "Endpoint not found")

    def send_head(self):
        path = self.translate_path(self.path)
        f = None
        if os.path.isdir(path):
            return super().send_head()

        try:
            f = open(path, 'rb')
        except OSError:
            self.send_error(404, "File not found")
            return None

        fs = os.fstat(f.fileno())
        file_len = fs[6]

        range_header = self.headers.get('Range')
        if range_header and range_header.startswith('bytes='):
            try:
                ranges = range_header.split('=', 1)[1].split('-')
                start = int(ranges[0]) if ranges[0] else 0
                end = int(ranges[1]) if ranges[1] else file_len - 1
                if start >= file_len:
                    self.send_error(416, "Requested Range Not Satisfiable")
                    f.close()
                    return None
                end = min(end, file_len - 1)
                length = end - start + 1

                self.send_response(206)
                self.send_header("Content-type", self.guess_type(path))
                self.send_header("Content-Range", f"bytes {start}-{end}/{file_len}")
                self.send_header("Content-Length", str(length))
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Last-Modified", self.date_time_string(fs.st_mtime))
                self.end_headers()

                f.seek(start)
                self.range_length = length
                return f
            except Exception:
                pass

        self.send_response(200)
        self.send_header("Content-type", self.guess_type(path))
        self.send_header("Content-Length", str(file_len))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Last-Modified", self.date_time_string(fs.st_mtime))
        self.end_headers()
        self.range_length = file_len
        return f

    def copyfile(self, source, outputfile):
        if hasattr(self, 'range_length') and self.range_length is not None:
            bufsize = 64 * 1024
            remaining = self.range_length
            try:
                while remaining > 0:
                    chunk = source.read(min(bufsize, remaining))
                    if not chunk:
                        break
                    outputfile.write(chunk)
                    remaining -= len(chunk)
            except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError, OSError):
                # Browser closed or aborted the connection during seeking or track change
                pass
        else:
            try:
                super().copyfile(source, outputfile)
            except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError, OSError):
                pass

def start_server():
    handler = MyHTTPRequestHandler
    with ThreadedHTTPServer(("", PORT), handler) as httpd:
        print(f"Local Server started at http://localhost:{PORT}")
        print("Press Ctrl+C in terminal to stop the server.")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nShutting down server...")
            sys.exit(0)

if __name__ == "__main__":
    server_thread = threading.Thread(target=start_server, daemon=True)
    server_thread.start()
    
    time.sleep(1.0)
    
    print("Opening Offline Video Player in browser...")
    webbrowser.open(f"http://localhost:{PORT}/index.html")
    
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nShutting down server...")
        sys.exit(0)
