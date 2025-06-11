#!/usr/bin/env python3
import os
import subprocess
import argparse
import re
import time
import threading
import json
from pathlib import Path
import tempfile
import logging
import shlex
from flask import (
    Flask,
    send_file,
    abort,
    render_template_string,
    request,
    redirect,
    url_for,
    jsonify,
)
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
from datetime import datetime
from functools import lru_cache
import difflib
from dotenv import load_dotenv  # Added dotenv import

# Set up logging
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("markdown-server")
logging.getLogger("werkzeug").setLevel(logging.WARNING)

app = Flask(__name__)

# Default configuration
config = {
    "notes_dir": os.path.expanduser("~/notes"),
    "pandoc_flags": [],
    "temp_dir": tempfile.gettempdir(),
    "default_extensions": [".md", ".markdown", ".txt"],
    "css_file": None,
    "refresh_interval": 500,  # milliseconds
}

# HTML cache to store rendered files and their modification times
html_cache = {}
# Track modified files
modified_files = set()
# Store CSS content
css_content = None


class MarkdownFileHandler(FileSystemEventHandler):
    """Handle file system events for markdown files."""

    def on_modified(self, event):
        if not event.is_directory:
            file_path = event.src_path
            if any(file_path.endswith(ext) for ext in config["default_extensions"]):
                logger.info(f"File modified: {file_path}")
                # Mark the file as modified
                modified_files.add(os.path.abspath(file_path))
                # Clear the cache for this file
                if file_path in html_cache:
                    logger.info(f"Clearing cache for {file_path}")
                    del html_cache[file_path]

            # If the CSS file was modified, reload it
            if config["css_file"] and os.path.abspath(file_path) == os.path.abspath(
                config["css_file"]
            ):
                logger.info(f"CSS file modified: {file_path}")
                load_css_content()
                # Clear all cache to regenerate with new CSS
                html_cache.clear()

            # If .env file was modified, reload environment variables
            if os.path.basename(file_path) == ".env":
                logger.info(".env file modified, reloading environment variables")
                load_dotenv(override=True)
                # Update pandoc flags
                config["pandoc_flags"] = parse_env_pandoc_flags()
                # Clear all cache to regenerate with new flags
                html_cache.clear()

    def on_created(self, event):
        if not event.is_directory:
            file_path = event.src_path
            if any(file_path.endswith(ext) for ext in config["default_extensions"]):
                logger.info(f"File created: {file_path}")

    def on_deleted(self, event):
        if not event.is_directory:
            file_path = event.src_path
            if any(file_path.endswith(ext) for ext in config["default_extensions"]):
                logger.info(f"File deleted: {file_path}")
                # Clear the cache for this file
                if file_path in html_cache:
                    logger.info(f"Clearing cache for {file_path}")
                    del html_cache[file_path]


def load_css_content():
    """Load CSS content from file."""
    global css_content
    if config["css_file"] and os.path.isfile(config["css_file"]):
        try:
            with open(config["css_file"], "r", encoding="utf-8") as f:
                css_content = f.read()
            logger.info(f"Loaded CSS from {config['css_file']}")
        except Exception as e:
            logger.error(f"Failed to load CSS file: {e}")
            css_content = None
    else:
        css_content = None


def find_note(note_path):
    """Find a note file by path or ID."""
    # Try direct path first
    full_path = os.path.join(config["notes_dir"], note_path)
    if os.path.isfile(full_path):
        _, ext = os.path.splitext(full_path)
        if ext in config["default_extensions"]:
            return full_path

    # If it doesn't have an extension, try adding default extensions
    if not os.path.splitext(full_path)[1]:
        for ext in config["default_extensions"]:
            test_path = full_path + ext
            if os.path.isfile(test_path):
                return test_path

    # Try ID-based lookup (search all files for matching name in notes_dir)
    for root, _, files in os.walk(config["notes_dir"]):
        for filename in files:
            name, ext = os.path.splitext(filename)
            if ext in config["default_extensions"] and name == note_path:
                return os.path.join(root, filename)

    return None


@lru_cache(maxsize=100)
def extract_title_from_markdown(file_path):
    """Extract title from a markdown file.

    Tries to find:
    1. YAML front matter title
    2. First heading
    3. Falls back to filename
    """
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            content = f.read()

        # Try to find YAML front matter title
        yaml_match = re.search(
            r"^---\s+$(.*?)^---\s+$", content, re.MULTILINE | re.DOTALL
        )
        if yaml_match:
            yaml_content = yaml_match.group(1)
            title_match = re.search(r"^title:\s*(.+)$", yaml_content, re.MULTILINE)
            if title_match:
                return title_match.group(1).strip().strip("\"'")

        # Try to find first heading
        heading_match = re.search(r"^#\s+(.+)$", content, re.MULTILINE)
        if heading_match:
            return heading_match.group(1).strip()

        # Fall back to filename
        return os.path.splitext(os.path.basename(file_path))[0]
    except Exception as e:
        logger.warning(f"Failed to extract title from {file_path}: {e}")
        return os.path.splitext(os.path.basename(file_path))[0]


def get_file_modification_time(file_path):
    """Get the modification time of a file."""
    return os.path.getmtime(file_path)


def inject_css_and_refresh(html_path, md_path):
    """Inject CSS content and auto-refresh script into HTML file."""
    try:
        with open(html_path, "r", encoding="utf-8") as file:
            content = file.read()

        # Convert to relative path for the API endpoint
        rel_path = os.path.relpath(md_path, config["notes_dir"])
        rel_path = rel_path.replace(os.sep, "/")  # Normalize path separators for URL

        # Script to check for updates and refresh the page
        refresh_script = f"""
        <script>
        (function() {{
            // Function to check if the file has been modified
            function checkForChanges() {{
                fetch('/api/check_modified/{rel_path}')
                    .then(response => response.json())
                    .then(data => {{
                        if (data.modified) {{
                            console.log('File has been modified, refreshing...');
                            window.location.reload();
                        }} else {{
                            setTimeout(checkForChanges, {config["refresh_interval"]});
                        }}
                    }})
                    .catch(error => {{
                        console.error('Error checking for file changes:', error);
                        setTimeout(checkForChanges, {config["refresh_interval"]});
                    }});
            }}
            
            // Start checking for changes
            setTimeout(checkForChanges, {config["refresh_interval"]});
        }})();
        </script>
        """

        # Add CSS if available
        if css_content:
            css_style = f"<style>\n{css_content}\n</style>"

            # Try to inject CSS into head
            if "</head>" in content:
                content = content.replace("</head>", f"{css_style}</head>")
            else:
                # If no head tag, insert at the beginning
                content = f"{css_style}\n{content}"

        # Insert the refresh script before the closing </body> tag if it exists
        if "</body>" in content:
            content = content.replace("</body>", f"{refresh_script}</body>")
        else:
            # If no </body> tag, append it to the end
            content += refresh_script

        with open(html_path, "w", encoding="utf-8") as file:
            file.write(content)

    except Exception as e:
        logger.error(f"Error injecting CSS and refresh script: {e}")


def convert_md_to_html(md_path):
    """Convert markdown to HTML using pandoc."""
    # Check if we have a valid cached version
    if md_path in html_cache:
        html_path, mod_time = html_cache[md_path]
        current_mod_time = get_file_modification_time(md_path)

        # If the file hasn't been modified since we cached it, return the cached version
        if current_mod_time <= mod_time and os.path.exists(html_path):
            logger.info(f"Using cached HTML for {md_path}")
            return html_path

    # Generate a unique output path
    html_path = os.path.join(
        config["temp_dir"], f"{os.path.basename(md_path)}_{int(time.time())}.html"
    )

    # Build pandoc command - don't include CSS, we'll inject it manually
    cmd = ["pandoc"] + config["pandoc_flags"] + ["-o", html_path, md_path]

    logger.info(f"Running pandoc command: {' '.join(cmd)}")

    try:
        subprocess.run(cmd, check=True)

        # Inject CSS and auto-refresh script
        inject_css_and_refresh(html_path, md_path)

        # Cache the result
        html_cache[md_path] = (html_path, get_file_modification_time(md_path))

        # Remove this file from the modified set if it's there
        abs_path = os.path.abspath(md_path)
        if abs_path in modified_files:
            modified_files.remove(abs_path)

        return html_path
    except subprocess.CalledProcessError as e:
        logger.error(f"Pandoc conversion failed: {e}")
        return None


def fuzzy_match(query, text):
    """Perform fuzzy matching between query and text."""
    query = query.lower()
    text = text.lower()

    # Direct substring match
    if query in text:
        return True

    # Fuzzy match using difflib
    seq_matcher = difflib.SequenceMatcher(None, query, text)
    ratio = seq_matcher.ratio()

    # Adjust threshold based on query length
    threshold = 0.6
    if len(query) <= 3:
        threshold = 0.8
    elif len(query) <= 5:
        threshold = 0.7

    return ratio >= threshold


def get_file_info(path, item):
    """Get information about a file or directory."""
    item_path = os.path.join(path, item)
    item_rel_path = os.path.relpath(item_path, config["notes_dir"])
    item_rel_path = item_rel_path.replace(
        os.sep, "/"
    )  # Normalize path separators for URL

    is_dir = os.path.isdir(item_path)

    if is_dir:
        title = item
        mod_time = os.path.getmtime(item_path)
    elif any(item.endswith(ext) for ext in config["default_extensions"]):
        title = extract_title_from_markdown(item_path)
        mod_time = os.path.getmtime(item_path)
    else:
        return None  # Skip non-markdown files

    return {
        "name": item,
        "path": f"/{item_rel_path}",
        "is_dir": is_dir,
        "title": title,
        "mod_time": mod_time,
        "mod_time_str": datetime.fromtimestamp(mod_time).strftime("%Y-%m-%d %H:%M:%S"),
    }


def generate_directory_listing(path):
    """Generate HTML for directory listing with sorting and search."""
    rel_path = os.path.relpath(path, config["notes_dir"])
    if rel_path == ".":
        rel_path = ""

    # Get query parameters for sorting and searching
    sort_by = request.args.get("sort", "name")  # Default sort by name
    sort_dir = request.args.get("dir", "asc")  # Default ascending
    search_query = request.args.get("q", "")

    items = []

    # Add parent directory link if not at root
    if rel_path:
        parent = os.path.dirname(rel_path)
        parent_item = {
            "name": "..",
            "path": f"/{parent}" if parent else "/",
            "is_dir": True,
            "title": "Parent Directory",
            "mod_time": 0,
            "mod_time_str": "",
        }
        items.append(parent_item)

    # List all directories and markdown files
    for item in os.listdir(path):
        # Skip hidden files
        if item.startswith("."):
            continue

        item_info = get_file_info(path, item)
        if item_info:
            # Only include if it matches search query (if any)
            if (
                not search_query
                or fuzzy_match(search_query, item_info["name"])
                or fuzzy_match(search_query, item_info["title"])
            ):
                items.append(item_info)

    # Sort the items
    if sort_by == "name":
        items.sort(key=lambda x: (not x["is_dir"], x["name"].lower()))
    elif sort_by == "title":
        items.sort(key=lambda x: (not x["is_dir"], x["title"].lower()))
    elif sort_by == "modified":
        items.sort(key=lambda x: (not x["is_dir"], x["mod_time"]))

    # Apply sort direction
    if sort_dir == "desc" and len(items) > 0 and items[0]["name"] != "..":
        # Don't reverse the parent directory
        parent_dir = None
        if items and items[0]["name"] == "..":
            parent_dir = items[0]
            items = items[1:]

        items.reverse()

        if parent_dir:
            items.insert(0, parent_dir)

    # Render the directory listing template
    html = """
    <!DOCTYPE html>
    <html>
    <head>
        <title>Notes Directory: {{ path }}</title>
        <style>
            body { font-family: Arial, sans-serif; margin: 20px; line-height: 1.6; color: #333; }
            h1 { color: #333; margin-bottom: 20px; }
            ul { list-style-type: none; padding: 0; }
            li { margin: 8px 0; display: flex; align-items: center; padding: 5px; border-bottom: 1px solid #eee; }
            li:hover { background-color: #f8f8f8; }
            a { text-decoration: none; color: #0366d6; flex-grow: 1; }
            a:hover { text-decoration: underline; }
            .folder:before { content: "📁 "; }
            .file:before { content: "📄 "; }
            .breadcrumbs { margin-bottom: 20px; background-color: #f5f5f5; padding: 10px; border-radius: 4px; }
            .breadcrumbs a { margin: 0 5px; color: #0366d6; }
            .filename { color: #666; font-size: 0.8em; margin-left: 10px; }
            .mod-time { color: #666; font-size: 0.8em; margin-left: 10px; min-width: 150px; text-align: right; }
            .search-sort-container { 
                display: flex; 
                gap: 10px; 
                margin-bottom: 20px; 
                align-items: center;
                flex-wrap: wrap;
            }
            .search-container { 
                flex-grow: 1; 
                position: relative;
                max-width: 500px;
            }
            .search-container input { 
                width: 100%; 
                padding: 8px; 
                border: 1px solid #ddd; 
                border-radius: 4px;
                font-size: 14px;
            }
            .search-container button {
                position: absolute;
                right: 8px;
                top: 50%;
                transform: translateY(-50%);
                background: none;
                border: none;
                cursor: pointer;
                color: #666;
            }
            .sort-container {
                display: flex;
                gap: 10px;
                align-items: center;
            }
            .sort-container select, .sort-container button {
                padding: 6px 10px;
                border: 1px solid #ddd;
                border-radius: 4px;
                background-color: white;
                font-size: 14px;
            }
            .sort-container button {
                cursor: pointer;
            }
            .sort-container button:hover {
                background-color: #f0f0f0;
            }
            .header { 
                display: flex; 
                justify-content: space-between; 
                align-items: center;
                margin-bottom: 20px;
            }
            .header h1 {
                margin: 0;
            }
            .column-headers {
                display: flex;
                padding: 5px;
                border-bottom: 2px solid #ddd;
                font-weight: bold;
                margin-bottom: 10px;
            }
            .name-column {
                flex-grow: 1;
            }
            .modified-column {
                min-width: 150px;
                text-align: right;
            }
            .sort-indicator {
                margin-left: 5px;
            }
            .sort-link {
                cursor: pointer;
                color: #333;
                text-decoration: none;
            }
            .sort-link:hover {
                text-decoration: underline;
            }
        </style>
        {% if custom_css %}
        <style>
            {{ custom_css }}
        </style>
        {% endif %}
    </head>
    <body>
        <div class="header">
            <h1>Directory: {{ path or '/' }}</h1>
        </div>
        
        <div class="breadcrumbs">
            <a href="/">Home</a>
            {% set breadcrumb_path = "" %}
            {% for part in path.split('/') %}
                {% if part %}
                    {% set breadcrumb_path = breadcrumb_path + "/" + part %}
                    &gt; <a href="{{ breadcrumb_path }}">{{ part }}</a>
                {% endif %}
            {% endfor %}
        </div>
        
        <div class="search-sort-container">
            <div class="search-container">
                <form id="search-form" action="{{ request.path }}" method="get">
                    <input type="text" name="q" id="search-input" placeholder="Search files and folders..." 
                           value="{{ search_query }}">
                    <input type="hidden" name="sort" value="{{ sort_by }}">
                    <input type="hidden" name="dir" value="{{ sort_dir }}">
                    <button type="submit">🔍</button>
                </form>
            </div>
            <div class="sort-container">
                <span>Sort by:</span>
                <a class="sort-link" href="?sort=name&dir={% if sort_by == 'name' and sort_dir == 'asc' %}desc{% else %}asc{% endif %}{% if search_query %}&q={{ search_query }}{% endif %}">
                    Name
                    {% if sort_by == 'name' %}
                        <span class="sort-indicator">{{ '▼' if sort_dir == 'desc' else '▲' }}</span>
                    {% endif %}
                </a>
                <a class="sort-link" href="?sort=title&dir={% if sort_by == 'title' and sort_dir == 'asc' %}desc{% else %}asc{% endif %}{% if search_query %}&q={{ search_query }}{% endif %}">
                    Title
                    {% if sort_by == 'title' %}
                        <span class="sort-indicator">{{ '▼' if sort_dir == 'desc' else '▲' }}</span>
                    {% endif %}
                </a>
                <a class="sort-link" href="?sort=modified&dir={% if sort_by == 'modified' and sort_dir == 'asc' %}desc{% else %}asc{% endif %}{% if search_query %}&q={{ search_query }}{% endif %}">
                    Modified
                    {% if sort_by == 'modified' %}
                        <span class="sort-indicator">{{ '▼' if sort_dir == 'desc' else '▲' }}</span>
                    {% endif %}
                </a>
            </div>
        </div>
        
        <div class="column-headers">
            <div class="name-column">Name</div>
            <div class="modified-column">Modified</div>
        </div>
        
        <ul>
            {% for item in items %}
                <li>
                    <a href="{{ item.path }}" class="{{ 'folder' if item.is_dir else 'file' }}">
                        {{ item.title }}
                        {% if not item.is_dir and item.name != item.title %}
                            <span class="filename">({{ item.name }})</span>
                        {% endif %}
                    </a>
                    {% if item.name != '..' %}
                        <span class="mod-time">{{ item.mod_time_str }}</span>
                    {% endif %}
                </li>
            {% endfor %}
        </ul>
        
        <script>
            // Client-side fuzzy search
            document.addEventListener('DOMContentLoaded', function() {
                const searchInput = document.getElementById('search-input');
                if (searchInput) {
                    // Auto-focus the search input if it's not empty
                    if (searchInput.value) {
                        searchInput.focus();
                    }
                    
                    // Optional: Real-time search with debounce
                    let debounceTimer;
                    searchInput.addEventListener('input', function() {
                        clearTimeout(debounceTimer);
                        debounceTimer = setTimeout(function() {
                            document.getElementById('search-form').submit();
                        }, 300);
                    });
                }
            });
            
            // Auto-refresh the page to check for new files (less frequently for directory listings)
            setTimeout(function() {
                window.location.reload();
            }, 30000); // Refresh every 30 seconds
        </script>
    </body>
    </html>
    """

    return render_template_string(
        html,
        path=rel_path,
        items=items,
        custom_css=css_content,
        search_query=search_query,
        sort_by=sort_by,
        sort_dir=sort_dir,
    )


@app.route("/api/search")
def api_search():
    """API endpoint for searching notes."""
    query = request.args.get("q", "")
    path = request.args.get("path", "")

    if not query:
        return jsonify([])

    # Get the full path
    full_path = os.path.join(config["notes_dir"], path)

    results = []

    # Walk through the directory and find matching files
    for root, dirs, files in os.walk(full_path):
        # Skip hidden directories
        dirs[:] = [d for d in dirs if not d.startswith(".")]

        for file in files:
            if any(file.endswith(ext) for ext in config["default_extensions"]):
                file_path = os.path.join(root, file)
                rel_path = os.path.relpath(file_path, config["notes_dir"])

                # Get the title
                title = extract_title_from_markdown(file_path)

                # Check if the file matches the query
                if fuzzy_match(query, file) or fuzzy_match(query, title):
                    results.append(
                        {
                            "path": f"/{rel_path.replace(os.sep, '/')}",
                            "name": file,
                            "title": title,
                            "is_dir": False,
                            "mod_time": os.path.getmtime(file_path),
                            "mod_time_str": datetime.fromtimestamp(
                                os.path.getmtime(file_path)
                            ).strftime("%Y-%m-%d %H:%M:%S"),
                        }
                    )

    # Sort results by relevance (exact matches first, then fuzzy matches)
    results.sort(
        key=lambda x: (
            0 if query.lower() in x["name"].lower() else 1,
            0 if query.lower() in x["title"].lower() else 1,
            x["name"].lower(),
        )
    )

    return jsonify(results)


@app.route("/api/check_modified/<path:path>")
def check_modified(path):
    """API endpoint to check if a file has been modified."""
    full_path = os.path.join(config["notes_dir"], path)
    abs_path = os.path.abspath(full_path)

    is_modified = abs_path in modified_files
    return jsonify({"modified": is_modified})


@app.route("/", defaults={"path": ""})
@app.route("/<path:path>")
def serve_note(path):
    """Serve a markdown note as HTML."""
    if not path:
        # Serve the root directory
        return generate_directory_listing(config["notes_dir"])

    # Check if path is a directory
    dir_path = os.path.join(config["notes_dir"], path)
    if os.path.isdir(dir_path):
        return generate_directory_listing(dir_path)

    # Try to find the note
    note_path = find_note(path)
    if not note_path:
        abort(404)

    # Convert markdown to HTML
    html_path = convert_md_to_html(note_path)
    if not html_path:
        abort(500)

    # Set headers to prevent caching
    response = send_file(html_path)
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


def parse_env_pandoc_flags():
    """Parse pandoc flags from environment variable or .env file."""
    # Environment variables already loaded from .env via load_dotenv() in main()
    env_flags = os.environ.get("PANDOC_FLAGS")
    if not env_flags:
        return config["pandoc_flags"]

    # Split by spaces, but respect quoted strings
    return shlex.split(env_flags)


def find_dotenv_file():
    """Find the .env file in the current or parent directories."""
    current_dir = os.path.abspath(os.getcwd())

    # Check current directory first
    env_path = os.path.join(current_dir, ".env")
    if os.path.isfile(env_path):
        return env_path

    # Then check up to 3 parent directories
    for _ in range(3):
        parent_dir = os.path.dirname(current_dir)
        if parent_dir == current_dir:  # Reached root directory
            break
        current_dir = parent_dir
        env_path = os.path.join(current_dir, ".env")
        if os.path.isfile(env_path):
            return env_path

    # Fall back to default .env in the working directory
    return ".env"


def start_file_watcher():
    """Start a file watcher on the notes directory."""
    event_handler = MarkdownFileHandler()
    observer = Observer()
    observer.schedule(event_handler, config["notes_dir"], recursive=True)

    # Also watch the CSS file if specified
    if config["css_file"]:
        css_dir = os.path.dirname(os.path.abspath(config["css_file"]))
        if css_dir != config["notes_dir"]:  # Avoid duplicate watches
            observer.schedule(event_handler, css_dir, recursive=False)

    # Watch the .env file location
    env_file = find_dotenv_file()
    env_dir = os.path.dirname(os.path.abspath(env_file))
    if env_dir not in [config["notes_dir"], css_dir if config["css_file"] else None]:
        observer.schedule(event_handler, env_dir, recursive=False)
        logger.info(f"Watching for .env file changes in: {env_dir}")

    observer.start()
    logger.info(f"File watcher started for {config['notes_dir']}")
    if config["css_file"]:
        logger.info(f"Watching CSS file: {config['css_file']}")

    return observer


def main():
    """Main entry point for the application."""
    # Load environment variables from .env file
    dotenv_path = find_dotenv_file()
    load_dotenv(dotenv_path=dotenv_path)
    logger.info(f"Loaded environment variables from {dotenv_path}")

    # Get pandoc flags from environment (which includes .env)
    config["pandoc_flags"] = parse_env_pandoc_flags()

    parser = argparse.ArgumentParser(description="Markdown Notes Web Server")
    parser.add_argument(
        "--notes-dir", help="Root directory for notes", default=config["notes_dir"]
    )
    parser.add_argument(
        "--pandoc-flags",
        help="Flags to pass to pandoc (overrides PANDOC_FLAGS env var)",
        nargs="+",
        default=None,
    )
    parser.add_argument(
        "--css", help="Custom CSS file to apply to rendered HTML", default=None
    )
    parser.add_argument(
        "--refresh-interval",
        help="Interval for checking file changes (milliseconds)",
        type=int,
        default=config["refresh_interval"],
    )
    parser.add_argument("--port", help="Port to listen on", type=int, default=5000)
    parser.add_argument("--host", help="Host to bind to", default="127.0.0.1")
    parser.add_argument("--debug", action="store_true", help="Enable debug mode")
    parser.add_argument("--no-watch", action="store_true", help="Disable file watching")
    parser.add_argument("--env-file", help="Path to .env file", default=None)

    args = parser.parse_args()

    # If --env-file specified, load it again (overriding previous values)
    if args.env_file:
        load_dotenv(dotenv_path=args.env_file, override=True)
        logger.info(f"Loaded environment variables from {args.env_file}")
        # Update pandoc flags if they were in the .env
        if args.pandoc_flags is None:  # Only if not explicitly provided via args
            config["pandoc_flags"] = parse_env_pandoc_flags()

    # Update configuration
    config["notes_dir"] = os.path.abspath(os.path.expanduser(args.notes_dir))
    if args.pandoc_flags is not None:
        config["pandoc_flags"] = args.pandoc_flags
    config["css_file"] = (
        os.path.abspath(os.path.expanduser(args.css)) if args.css else None
    )
    config["refresh_interval"] = args.refresh_interval

    # Check if notes directory exists
    if not os.path.isdir(config["notes_dir"]):
        logger.error(f"Notes directory does not exist: {config['notes_dir']}")
        exit(1)

    # Check if CSS file exists and load content
    if config["css_file"] and not os.path.isfile(config["css_file"]):
        logger.error(f"CSS file does not exist: {config['css_file']}")
        exit(1)
    else:
        load_css_content()

    # Check if pandoc is installed
    try:
        subprocess.run(["pandoc", "--version"], check=True, capture_output=True)
    except (subprocess.CalledProcessError, FileNotFoundError):
        logger.error("Pandoc is not installed or not in PATH")
        exit(1)

    # Start file watcher
    observer = None
    if not args.no_watch:
        observer = start_file_watcher()

    logger.info(f"Starting server with notes directory: {config['notes_dir']}")
    logger.info(f"Pandoc flags: {' '.join(config['pandoc_flags'])}")
    if config["css_file"]:
        logger.info(f"Using CSS file: {config['css_file']}")

    try:
        app.run(host=args.host, port=args.port, debug=args.debug)
    except KeyboardInterrupt:
        logger.info("Shutting down server...")
    finally:
        if observer:
            observer.stop()
            observer.join()


if __name__ == "__main__":
    main()
