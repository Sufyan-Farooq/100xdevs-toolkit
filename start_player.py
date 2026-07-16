import http.server
import socketserver
import webbrowser
import threading
import time
import sys

PORT = 8000
DIRECTORY = "."

class MyHTTPRequestHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, format, *args):
        # Prevent spamming console with standard requests log
        pass

def start_server():
    # Configure server to bind to port 8000 and serve local files
    handler = MyHTTPRequestHandler
    # Allow address reuse to avoid port collision errors on rapid restarts
    socketserver.TCPServer.allow_reuse_address = True
    
    with socketserver.TCPServer(("", PORT), handler) as httpd:
        print(f"Local Server started at http://localhost:{PORT}")
        print("Press Ctrl+C in terminal to stop the server.")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nShutting down server...")
            sys.exit(0)

if __name__ == "__main__":
    # Start server in a background daemon thread
    server_thread = threading.Thread(target=start_server, daemon=True)
    server_thread.start()
    
    # Wait a moment for server to bind
    time.sleep(1.0)
    
    # Open player dashboard automatically in the user's browser
    print("Opening Offline Video Player in browser...")
    webbrowser.open(f"http://localhost:{PORT}/index.html")
    
    # Keep the main thread alive to serve requests
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nShutting down server...")
        sys.exit(0)
