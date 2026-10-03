#!/usr/bin/env python3
"""A private, dependency-free HTML library for Python 3.9+.

The management UI and original documents deliberately use separate origins.
Run ``python3 server.py --help`` for local configuration.
"""

import argparse
import hashlib
import html
import json
import mimetypes
import os
from pathlib import Path
import re
import secrets
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, unquote, urlsplit


APP_DIR = Path(__file__).resolve().parent
DEFAULT_ROOT = Path.home() / "Documents" / "Codex"
EXCLUDED_DIRS = {
    "work", "node_modules", ".git", ".venv", "venv", "templates", "scripts",
    "tests", "vendor", "cache", "__pycache__", "coverage",
}
MAX_PARSE_BYTES = 2 * 1024 * 1024
MAX_REQUEST_BYTES = 64 * 1024


def choose_macos_folder():
    """Show the user's native folder picker; None means they cancelled it."""
    if sys.platform != "darwin":
        raise ValueError("系统文件夹选择目前适用于 macOS。")
    script = ('activate\n'
              'POSIX path of (choose folder with prompt "选择要自动收录 HTML 的文件夹" '
              'default location (path to documents folder))')
    try:
        result = subprocess.run(
            ["/usr/bin/osascript", "-e", script],
            capture_output=True, text=True, timeout=300, check=False,
        )
    except subprocess.TimeoutExpired:
        raise ValueError("文件夹选择已超时，请重新选择。")
    if result.returncode:
        error = result.stderr.strip()
        if "-128" in error or "User canceled" in error or "用户已取消" in error:
            return None
        raise ValueError("系统文件夹选择窗口未能打开，请双击「开始使用.command」重新启动资料库后再试。")
    selected = result.stdout.rstrip("\r\n")
    if not selected:
        raise ValueError("没有收到选中的文件夹，请重试。")
    return selected
RESOURCE_EXTENSIONS = {
    ".html", ".htm", ".css", ".js", ".mjs", ".cjs", ".json", ".webmanifest",
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".avif", ".svg", ".ico", ".bmp",
    ".woff", ".woff2", ".ttf", ".otf", ".eot", ".wasm",
    ".mp3", ".mp4", ".m4a", ".ogg", ".ogv", ".wav", ".webm", ".mov", ".vtt",
    ".pdf", ".txt", ".md", ".csv", ".tsv", ".xml",
}


def stable_id(value):
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:24]


def normalize_text(value):
    return re.sub(r"\s+", " ", value).strip()


def contained(path, root):
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def read_html(path, limit=MAX_PARSE_BYTES):
    with open(str(path), "rb") as source:
        raw = source.read(limit)
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            pass
    return raw.decode("utf-8", errors="replace")


class DocumentText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title_parts = []
        self.body_parts = []
        self.headings = []
        self.description = ""
        self.in_title = False
        self.in_heading = False
        self.hidden_depth = 0

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        attrs = dict(attrs)
        if tag in ("script", "style", "noscript", "template"):
            self.hidden_depth += 1
        elif tag == "title":
            self.in_title = True
        elif tag in ("h1", "h2"):
            self.in_heading = True
        elif tag == "meta" and attrs.get("name", "").lower() == "description":
            self.description = attrs.get("content", "") or ""

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript", "template"):
            self.hidden_depth = max(0, self.hidden_depth - 1)
        elif tag == "title":
            self.in_title = False
        elif tag in ("h1", "h2"):
            self.in_heading = False

    def handle_data(self, data):
        if self.hidden_depth:
            return
        if self.in_title:
            self.title_parts.append(data)
        else:
            self.body_parts.append(data)
            if self.in_heading:
                self.headings.append(data)


def infer_category(title, filename, text):
    hint = (title + " " + filename).lower()
    categories = [
        ("演示文稿", ("presentation", "slide", "deck", "ppt", "演示", "幻灯", "汇报")),
        ("产品文档", ("prd", "需求", "产品文档", "方案", "设计规范")),
        ("数据报告", ("dashboard", "report", "数据", "报告", "分析", "看板")),
        ("交互原型", ("prototype", "mockup", "原型", "交互", "wireframe")),
        ("实用工具", ("calculator", "tool", "工具", "计算器", "转换", "生成器")),
        ("学习笔记", ("note", "笔记", "教程", "学习", "知识", "指南")),
    ]
    for category, words in categories:
        if any(word in hint for word in words):
            return category
    return "其他页面"


def document_info(path):
    parser = DocumentText()
    parser.feed(read_html(path))
    title = normalize_text(" ".join(parser.title_parts))
    if not title:
        title = normalize_text(" ".join(parser.headings))[:140] or path.stem
    title = title[:300]
    text = normalize_text(" ".join(parser.body_parts))
    description = normalize_text(parser.description)[:240] or text[:240]
    return title, description, text, infer_category(title, path.name, text)


def project_name(relative, root_name):
    parts = list(relative.parts[:-1])
    ignored = {"outputs", "output", "html", "new-chat", "artifacts", "pages", "原型", "需求文档", "流程图", "public", "dist", "build"}
    useful = [p for p in parts if p.lower() not in ignored and not re.fullmatch(r"\d{4}[-_]\d{2}[-_]\d{2}", p)]
    return useful[-1] if useful else root_name


def blocked_resource_path(path, root):
    """Check the resolved path too: a visible symlink must not expose dotfiles."""
    if not contained(path, root):
        return True
    return any(part.startswith(".") for part in path.relative_to(root).parts)


def root_available(path):
    """A registered root must not silently become a different symlink target."""
    try:
        return path.is_dir() and path.resolve() == path
    except (OSError, RuntimeError):
        return False


class RootResourceRewriter(HTMLParser):
    """Resolve local /assets references without modifying the source on disk.

    Exported sites often assume they are hosted at a server root. Find the
    closest existing asset in the page's ancestor directories, then point the
    browser at its capability URL. Relative and external URLs stay intact.
    """

    def __init__(self, root, source, capability, data_dir):
        super().__init__(convert_charrefs=False)
        self.root = root
        self.source = source
        self.capability = capability
        self.data_dir = data_dir
        self.parts = []
        self.in_style = False

    def rewrite_url(self, value):
        if not value.startswith("/") or value.startswith("//"):
            return value
        parsed = urlsplit(html.unescape(value))
        relative = unquote(parsed.path).lstrip("/")
        if ".." in Path(relative).parts or "\\" in relative or "\x00" in relative:
            return value
        ancestor = self.source.parent
        while contained(ancestor, self.root):
            candidate = (ancestor / relative).resolve()
            if (not blocked_resource_path(candidate, self.root) and not contained(candidate, APP_DIR)
                    and not contained(candidate, self.data_dir) and candidate.is_file()):
                rewritten = "/files/" + self.capability + "/" + quote(candidate.relative_to(self.root).as_posix(), safe="/")
                if parsed.query:
                    rewritten += "?" + parsed.query
                if parsed.fragment:
                    rewritten += "#" + parsed.fragment
                return rewritten
            if ancestor == self.root:
                break
            ancestor = ancestor.parent
        return value

    def rewrite_css(self, value):
        pattern = r"(url\(\s*)([\"']?)(/[^)\"'\s]+)(\2\s*\))"
        value = re.sub(pattern, lambda m: m.group(1) + m.group(2) + self.rewrite_url(m.group(3)) + m.group(4), value, flags=re.I)
        return re.sub(r"(@import\s+)([\"'])(/[^\"']+)(\2)",
                      lambda m: m.group(1) + m.group(2) + self.rewrite_url(m.group(3)) + m.group(4), value, flags=re.I)

    def handle_starttag(self, tag, attrs):
        text = self.get_starttag_text()
        pattern = r"(\b(?:src|href|poster|action|data)\s*=\s*)([\"'])(/[^\"']*)(\2)"
        def rewrite_attribute(match):
            original = match.group(3)
            rewritten = self.rewrite_url(original)
            if rewritten == original:
                return match.group(0)
            return match.group(1) + match.group(2) + html.escape(rewritten, quote=True) + match.group(4)
        text = re.sub(pattern, rewrite_attribute, text, flags=re.I)
        self.parts.append(self.rewrite_css(text))
        if tag == "style":
            self.in_style = True

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        self.parts.append("</%s>" % tag)
        if tag == "style":
            self.in_style = False

    def handle_data(self, data):
        self.parts.append(self.rewrite_css(data) if self.in_style else data)

    def handle_entityref(self, name):
        self.parts.append("&%s;" % name)

    def handle_charref(self, name):
        self.parts.append("&#%s;" % name)

    def handle_comment(self, data):
        self.parts.append("<!--%s-->" % data)

    def handle_decl(self, decl):
        self.parts.append("<!%s>" % decl)

    def handle_pi(self, data):
        self.parts.append("<?%s>" % data)

    def unknown_decl(self, data):
        self.parts.append("<![%s]>" % data)


class SafePreview(HTMLParser):
    """Preserve static structure/styles; CSP is the final security boundary."""

    BLOCKED = {"script", "iframe", "object", "embed", "template", "noscript", "svg", "math"}
    ALLOWED = {
        "div", "span", "p", "section", "article", "main", "header", "footer", "nav", "aside",
        "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "li", "dl", "dt", "dd",
        "table", "thead", "tbody", "tfoot", "tr", "td", "th", "caption", "colgroup", "col",
        "pre", "code", "blockquote", "q", "b", "strong", "i", "em", "u", "s", "small",
        "br", "hr", "img", "figure", "figcaption", "details", "summary", "a", "button",
        "label", "kbd", "samp", "sub", "sup", "mark", "time", "address", "style",
    }
    VOID = {"br", "hr", "img", "col"}
    ATTRS = {"class", "id", "style", "title", "alt", "width", "height", "colspan", "rowspan", "open"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.block_stack = []
        self.in_style = False
        self.in_title = False

    @staticmethod
    def clean_css(value):
        value = re.sub(r"/\*.*?\*/", "", value, flags=re.S)
        value = re.sub(r"@import[^;]*;?", "", value, flags=re.I)
        value = re.sub(r"url\s*\([^)]*\)", "none", value, flags=re.I)
        # Escaped identifiers can conceal URL/import; omitting this style is safer.
        return "" if "\\" in value or "</" in value else value

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if self.block_stack:
            if tag in self.BLOCKED:
                self.block_stack.append(tag)
            return
        if tag in self.BLOCKED:
            if tag not in ("embed",):
                self.block_stack.append(tag)
            return
        if tag == "title":
            self.in_title = True
            return
        if tag not in self.ALLOWED:
            return
        safe = []
        for key, value in attrs:
            value = value or ""
            if key in self.ATTRS:
                if key == "style":
                    value = self.clean_css(value)
                safe.append('%s="%s"' % (key, html.escape(value, quote=True)))
            elif tag == "img" and key == "src" and re.match(r"^data:image/(png|jpeg|gif|webp);base64,", value, re.I):
                safe.append('src="%s"' % html.escape(value, quote=True))
        if tag == "button":
            safe.append('type="button" disabled')
        self.parts.append("<%s%s>" % (tag, (" " + " ".join(safe)) if safe else ""))
        if tag == "style":
            self.in_style = True

    def handle_endtag(self, tag):
        if self.block_stack:
            if tag == self.block_stack[-1]:
                self.block_stack.pop()
            return
        if tag == "title":
            self.in_title = False
        if tag in self.ALLOWED and tag not in self.VOID:
            self.parts.append("</%s>" % tag)
        if tag == "style":
            self.in_style = False

    def handle_data(self, data):
        if self.block_stack or self.in_title:
            return
        self.parts.append(self.clean_css(data) if self.in_style else html.escape(data))


class Library:
    def __init__(self, data_dir, app_port=18765, content_port=18766, roots=None, interval=10):
        self.data_dir = Path(data_dir).expanduser().resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.data_dir / "library.sqlite3"
        self.lock = threading.RLock()
        self.folder_picker_lock = threading.Lock()
        self.db = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS roots (
                id TEXT PRIMARY KEY, path TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
                capability TEXT NOT NULL UNIQUE
            );
            CREATE TABLE IF NOT EXISTS items (
                id TEXT PRIMARY KEY, path TEXT NOT NULL UNIQUE, root_id TEXT NOT NULL,
                relative_path TEXT NOT NULL, filename TEXT NOT NULL, parsed_title TEXT NOT NULL,
                title_override TEXT, project TEXT NOT NULL, category TEXT NOT NULL,
                description TEXT NOT NULL, body_text TEXT NOT NULL, mtime REAL NOT NULL,
                mtime_ns INTEGER NOT NULL, size INTEGER NOT NULL, favorite INTEGER NOT NULL DEFAULT 0,
                last_opened REAL, present INTEGER NOT NULL DEFAULT 1
            );
            CREATE INDEX IF NOT EXISTS items_root ON items(root_id);
        """)
        self.db.commit()
        try:
            os.chmod(str(self.data_dir), 0o700)
            os.chmod(str(self.db_path), 0o600)
        except OSError:
            pass
        self.app_port = app_port
        self.content_port = content_port
        self.app_origin = "http://127.0.0.1:%d" % app_port
        self.content_origin = "http://127.0.0.1:%d" % content_port
        self.csrf_token = secrets.token_urlsafe(32)
        self.interval = max(1, float(interval))
        self.scan_status = {"running": False, "last_scan": None, "interval": self.interval, "error": None}
        self.stop_event = threading.Event()
        self.scan_event = threading.Event()
        self.worker = None
        initialized = self.db.execute("SELECT value FROM settings WHERE key='initialized'").fetchone()
        if roots:
            for root in roots:
                self.add_root(root)
        elif not initialized and DEFAULT_ROOT.is_dir():
            self.add_root(str(DEFAULT_ROOT))
        self.db.execute("INSERT OR REPLACE INTO settings VALUES ('initialized','1')")
        self.db.commit()

    def add_root(self, value):
        if not isinstance(value, str) or not value.strip():
            raise ValueError("请输入文件夹的完整路径。")
        path = Path(os.path.expandvars(value.strip())).expanduser()
        if not path.is_absolute():
            raise ValueError("请使用绝对路径，例如 ~/Documents。")
        path = path.resolve()
        if path == Path(path.anchor):
            raise ValueError("请选择具体文件夹，不能扫描整个磁盘。")
        if not path.is_dir():
            raise ValueError("这个文件夹不存在或无法访问。")
        if path == APP_DIR or contained(path, self.data_dir):
            raise ValueError("请选择存放 HTML 的文件夹，而非资料库程序目录。")
        root_id = stable_id(path)
        with self.lock:
            self.db.execute(
                "INSERT OR IGNORE INTO roots(id,path,name,capability) VALUES (?,?,?,?)",
                (root_id, str(path), path.name, secrets.token_urlsafe(24)),
            )
            self.db.commit()
        self.scan_event.set()
        return root_id

    def remove_root(self, root_id):
        with self.lock:
            self.db.execute("DELETE FROM items WHERE root_id=?", (root_id,))
            deleted = self.db.execute("DELETE FROM roots WHERE id=?", (root_id,)).rowcount
            self.db.commit()
        return bool(deleted)

    def start(self):
        self.worker = threading.Thread(target=self._scan_loop, name="html-library-scanner", daemon=True)
        self.worker.start()

    def close(self):
        self.stop_event.set()
        self.scan_event.set()
        if self.worker:
            self.worker.join(timeout=15)
        with self.lock:
            self.db.close()

    def _scan_loop(self):
        while not self.stop_event.is_set():
            self.scan_event.clear()
            try:
                self.scan_once()
            except Exception as exc:
                with self.lock:
                    self.scan_status.update(running=False, error="扫描未完成：%s" % str(exc))
            self.scan_event.wait(self.interval)

    def scan_once(self):
        with self.lock:
            if self.scan_status["running"]:
                return
            self.scan_status.update(running=True, error=None)
            roots = [dict(row) for row in self.db.execute("SELECT * FROM roots ORDER BY length(path) DESC")]
            existing = {row["path"]: dict(row) for row in self.db.execute("SELECT id,path,mtime_ns,size,root_id,present FROM items")}
        errors = []
        seen_all = set()
        try:
            for root in roots:
                if self.stop_event.is_set():
                    break
                root_path = Path(root["path"])
                seen = set()
                walk_errors = []
                if not root_available(root_path):
                    errors.append("文件夹暂不可用：%s" % root["name"])
                    with self.lock:
                        self.db.execute("UPDATE items SET present=0 WHERE root_id=?", (root["id"],))
                        self.db.commit()
                    continue
                for directory, dirs, files in os.walk(str(root_path), followlinks=False, onerror=walk_errors.append):
                    if self.stop_event.is_set():
                        break
                    directory_path = Path(directory)
                    dirs[:] = sorted(d for d in dirs if not d.startswith(".") and d.lower() not in EXCLUDED_DIRS
                                     and not (directory_path / d).is_symlink()
                                     and (directory_path / d).resolve() != APP_DIR
                                     and not contained((directory_path / d).resolve(), self.data_dir))
                    for filename in files:
                        if self.stop_event.is_set():
                            break
                        if filename.startswith(".") or Path(filename).suffix.lower() not in (".html", ".htm"):
                            continue
                        source = directory_path / filename
                        try:
                            if source.is_symlink():
                                continue
                            path = source.resolve()
                            if not contained(path, root_path) or contained(path, APP_DIR) or str(path) in seen_all:
                                continue
                            stat = path.stat()
                            previous = existing.get(str(path))
                            item_id = stable_id(path)
                            seen.add(item_id)
                            seen_all.add(str(path))
                            if previous and previous["mtime_ns"] == stat.st_mtime_ns and previous["size"] == stat.st_size and previous["root_id"] == root["id"]:
                                if not previous["present"]:
                                    with self.lock:
                                        self.db.execute("UPDATE items SET present=1 WHERE id=?", (item_id,))
                                        self.db.commit()
                                continue
                            title, description, body, category = document_info(path)
                            relative = path.relative_to(root_path)
                            values = (item_id, str(path), root["id"], relative.as_posix(), filename, title,
                                      project_name(relative, root["name"]), category, description, body,
                                      stat.st_mtime, stat.st_mtime_ns, stat.st_size)
                            with self.lock:
                                if not self.db.execute("SELECT 1 FROM roots WHERE id=?", (root["id"],)).fetchone():
                                    continue
                                self.db.execute("""
                                    INSERT INTO items(id,path,root_id,relative_path,filename,parsed_title,project,
                                        category,description,body_text,mtime,mtime_ns,size)
                                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                                    ON CONFLICT(id) DO UPDATE SET root_id=excluded.root_id,
                                        relative_path=excluded.relative_path,filename=excluded.filename,
                                        parsed_title=excluded.parsed_title,project=excluded.project,
                                        category=excluded.category,description=excluded.description,
                                        body_text=excluded.body_text,mtime=excluded.mtime,mtime_ns=excluded.mtime_ns,
                                        size=excluded.size,present=1
                                """, values)
                                self.db.commit()
                        except (OSError, ValueError) as exc:
                            errors.append("无法读取 %s：%s" % (filename, str(exc)))
                if walk_errors:
                    errors.append("部分子文件夹无法访问：%s" % root["name"])
                elif not self.stop_event.is_set():
                    with self.lock:
                        current = self.db.execute("SELECT id FROM items WHERE root_id=? AND present=1", (root["id"],)).fetchall()
                        missing = [(row["id"],) for row in current if row["id"] not in seen]
                        self.db.executemany("UPDATE items SET present=0 WHERE id=?", missing)
                        self.db.commit()
        finally:
            with self.lock:
                self.scan_status.update(running=False, last_scan=time.time(), error="；".join(errors[:6]) or None)

    def state(self, query=""):
        with self.lock:
            root_rows = [dict(row) for row in self.db.execute("SELECT id,path,name,capability FROM roots ORDER BY name")]
            capabilities = {root["id"]: root.pop("capability") for root in root_rows}
            roots = {root["id"]: root for root in root_rows}
            for root in root_rows:
                root["exists"] = root_available(Path(root["path"]))
                root["count"] = self.db.execute("SELECT count(*) FROM items WHERE root_id=? AND present=1", (root["id"],)).fetchone()[0]
            sql = "SELECT id,path,root_id,relative_path,filename,COALESCE(title_override,parsed_title) AS title,project,category,description,mtime,size,favorite,last_opened,present FROM items WHERE present=1"
            args = []
            for word in query.casefold().split():
                # instr() is literal and avoids treating % or _ as search wildcards.
                sql += " AND instr(lower(COALESCE(title_override,parsed_title)||' '||filename||' '||path||' '||body_text),?)>0"
                args.append(word)
            sql += " ORDER BY mtime DESC,path"
            items = []
            for row in self.db.execute(sql, args):
                item = dict(row)
                if item["root_id"] not in roots:
                    continue
                item["root_name"] = roots[item["root_id"]]["name"]
                item["url"] = self.content_origin + "/files/" + capabilities[item["root_id"]] + "/" + quote(item["relative_path"], safe="/")
                item["favorite"] = bool(item["favorite"])
                item["exists"] = bool(item.pop("present"))
                items.append(item)
            summary = self.db.execute("SELECT count(*),COALESCE(sum(favorite),0),COALESCE(sum(CASE WHEN last_opened IS NOT NULL THEN 1 ELSE 0 END),0) FROM items WHERE present=1").fetchone()
            return {"items": items, "roots": root_rows, "scan": dict(self.scan_status),
                    "stats": {"total": summary[0], "favorites": summary[1], "recent": summary[2]},
                    "content_origin": self.content_origin, "csrf_token": self.csrf_token}

    def get_item(self, item_id):
        with self.lock:
            row = self.db.execute("SELECT items.*, roots.path AS root_path, roots.capability FROM items JOIN roots ON items.root_id=roots.id WHERE items.id=?", (item_id,)).fetchone()
        if not row:
            raise KeyError("找不到这个页面。")
        return dict(row)

    def verified_item_path(self, item):
        path = Path(item["path"])
        root = Path(item["root_path"])
        if not root_available(root):
            raise FileNotFoundError("扫描文件夹已移动或变成符号链接，请重新添加真实文件夹。")
        resolved = path.resolve()
        if not contained(resolved, root) or path.is_symlink() or not resolved.is_file():
            raise FileNotFoundError("文件已移动、删除或暂时无法访问，请重新扫描。")
        return resolved

    def item_url(self, item_id, opened=False):
        item = self.get_item(item_id)
        self.verified_item_path(item)
        if opened:
            with self.lock:
                self.db.execute("UPDATE items SET last_opened=? WHERE id=?", (time.time(), item_id))
                self.db.commit()
        return self.content_origin + "/files/" + item["capability"] + "/" + quote(item["relative_path"], safe="/")

    def update_item(self, item_id, payload):
        self.get_item(item_id)
        assignments, values = [], []
        if "favorite" in payload:
            if not isinstance(payload["favorite"], bool):
                raise ValueError("favorite 必须是布尔值。")
            assignments.append("favorite=?")
            values.append(int(payload["favorite"]))
        if "title" in payload:
            title = payload["title"]
            if title is not None and not isinstance(title, str):
                raise ValueError("标题必须是文本。")
            assignments.append("title_override=?")
            values.append(normalize_text(title)[:300] or None if title is not None else None)
        if not assignments:
            raise ValueError("没有可更新的内容。")
        with self.lock:
            self.db.execute("UPDATE items SET %s WHERE id=?" % ",".join(assignments), values + [item_id])
            self.db.commit()

    def preview(self, item_id):
        item = self.get_item(item_id)
        path = self.verified_item_path(item)
        parser = SafePreview()
        parser.feed(read_html(path))
        content = "".join(parser.parts)
        title = item["title_override"] or item["parsed_title"]
        if not item["body_text"].strip():
            content = ("<main style=\"max-width:640px;margin:12vh auto;padding:24px\"><p style=\"font-size:12px;color:#70816e\">HTML 页面</p>"
                       "<h1>%s</h1><p style=\"color:#767b74\">这个页面没有可供静态预览的正文。打开原网页后可加载完整内容与交互。</p></main>" % html.escape(title))
        return ("<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
                "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
                "<title>%s</title><style>html{color-scheme:light}body{margin:24px;font-family:-apple-system,BlinkMacSystemFont,'PingFang SC',sans-serif;line-height:1.65;color:#30332f;background:#fff}img{max-width:100%%}a,button{pointer-events:none}*{animation:none!important;transition:none!important}</style>"
                "</head><body>%s</body></html>" % (html.escape(title), content)).encode("utf-8")


class LibraryHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, handler, library):
        self.library = library
        super().__init__(address, handler)


class BaseHandler(BaseHTTPRequestHandler):
    server_version = "HTMLLibrary/1.0"

    @property
    def library(self):
        return self.server.library

    def log_message(self, fmt, *args):
        # Keep polling out of the service log; retain unexpected failures.
        if len(args) >= 2 and str(args[1]).startswith("5"):
            super().log_message(fmt, *args)

    def valid_host(self):
        expected = "127.0.0.1:%d" % self.server.server_port
        if self.headers.get("Host") != expected:
            self.send_json({"error": "请使用 127.0.0.1 本地地址访问。"}, 403)
            return False
        return True

    def send_bytes(self, data, content_type, status=200, headers=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD":
            try:
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def send_json(self, value, status=200):
        self.send_bytes(json.dumps(value, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8", status,
                        {"Cross-Origin-Resource-Policy": "same-origin"})

    def fail(self, exc):
        if isinstance(exc, KeyError):
            self.send_json({"error": str(exc.args[0])}, 404)
        elif isinstance(exc, FileNotFoundError):
            self.send_json({"error": str(exc)}, 404)
        elif isinstance(exc, (ValueError, UnicodeError, json.JSONDecodeError)):
            self.send_json({"error": str(exc)}, 400)
        elif isinstance(exc, PermissionError):
            self.send_json({"error": "文件访问被系统拒绝。"}, 403)
        else:
            print("Request error: %s" % str(exc), file=sys.stderr, flush=True)
            self.send_json({"error": "操作未完成，请稍后重试。"}, 500)


class AppHandler(BaseHandler):
    def authorized_read(self):
        origin = self.headers.get("Origin")
        if origin and origin != self.library.app_origin:
            self.send_json({"error": "不允许其他网页访问资料库。"}, 403)
            return False
        if self.headers.get("Sec-Fetch-Site") in ("cross-site", "same-site"):
            self.send_json({"error": "请从资料库页面发起请求。"}, 403)
            return False
        return True

    def read_mutation(self):
        if self.headers.get("Origin") != self.library.app_origin:
            raise PermissionError("origin")
        token = self.headers.get("X-Library-Token", "")
        if not secrets.compare_digest(token, self.library.csrf_token):
            raise PermissionError("token")
        if self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
            raise ValueError("请求需使用 application/json。")
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise ValueError("无效的请求长度。")
        if length < 0 or length > MAX_REQUEST_BYTES:
            raise ValueError("请求内容过大。")
        data = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
        if not isinstance(data, dict):
            raise ValueError("请求内容必须是 JSON 对象。")
        return data

    def do_GET(self):
        if not self.valid_host():
            return
        parsed = urlsplit(self.path)
        route = parsed.path
        try:
            if route.startswith("/api/"):
                if not self.authorized_read():
                    return
                if route in ("/api/state", "/api/search"):
                    query = parse_qs(parsed.query).get("q", [""])[0][:1000] if route == "/api/search" else ""
                    self.send_json(self.library.state(query))
                    return
                match = re.fullmatch(r"/api/items/([a-f0-9]{24})/(preview|url)", route)
                if match:
                    item_id, operation = match.groups()
                    if operation == "url":
                        self.send_json({"url": self.library.item_url(item_id)})
                    else:
                        self.send_bytes(self.library.preview(item_id), "text/html; charset=utf-8", headers={
                            "Content-Security-Policy": "default-src 'none'; script-src 'none'; style-src 'unsafe-inline'; img-src data:; font-src 'none'; media-src 'none'; frame-src 'none'; connect-src 'none'; form-action 'none'; base-uri 'none'; sandbox; frame-ancestors 'self'",
                            "Cross-Origin-Resource-Policy": "same-origin",
                        })
                    return
                self.send_json({"error": "接口不存在。"}, 404)
                return
            assets = {"/": "index.html", "/index.html": "index.html", "/app.js": "app.js", "/styles.css": "styles.css"}
            if route == "/favicon.ico":
                self.send_bytes(b"", "image/x-icon", 204)
            elif route in assets:
                path = APP_DIR / assets[route]
                types = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8"}
                self.send_bytes(path.read_bytes(), types[path.suffix], headers={
                    "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'",
                    "Cross-Origin-Opener-Policy": "same-origin",
                })
            else:
                self.send_json({"error": "页面不存在。"}, 404)
        except Exception as exc:
            self.fail(exc)

    def do_HEAD(self):
        self.do_GET()

    def do_OPTIONS(self):
        if self.valid_host():
            self.send_json({"error": "不允许跨域请求。"}, 403)

    def mutate(self):
        if not self.valid_host():
            return
        try:
            if not self.authorized_read():
                return
            payload = self.read_mutation()
            route = urlsplit(self.path).path
            if self.command == "POST" and route == "/api/scan":
                self.library.scan_event.set()
                self.send_json({"ok": True})
            elif self.command == "POST" and route == "/api/roots":
                root_id = self.library.add_root(payload.get("path"))
                self.send_json({"ok": True, "id": root_id})
            elif self.command == "POST" and route == "/api/roots/choose":
                if not self.library.folder_picker_lock.acquire(blocking=False):
                    raise ValueError("文件夹选择窗口已经打开，请先完成当前选择。")
                try:
                    selected = choose_macos_folder()
                finally:
                    self.library.folder_picker_lock.release()
                if selected is None:
                    self.send_json({"ok": True, "cancelled": True})
                else:
                    root_id = self.library.add_root(selected)
                    self.send_json({"ok": True, "id": root_id, "name": Path(selected).name})
            elif self.command == "DELETE" and re.fullmatch(r"/api/roots/[a-f0-9]{24}", route):
                if not self.library.remove_root(route.rsplit("/", 1)[1]):
                    raise KeyError("找不到这个文件夹。")
                self.send_json({"ok": True})
            else:
                match = re.fullmatch(r"/api/items/([a-f0-9]{24})(?:/(open|reveal))?", route)
                if not match:
                    self.send_json({"error": "接口不存在。"}, 404)
                    return
                item_id, action = match.groups()
                if self.command == "PATCH" and not action:
                    self.library.update_item(item_id, payload)
                    self.send_json({"ok": True})
                elif self.command == "POST" and action == "open":
                    self.send_json({"url": self.library.item_url(item_id, opened=True)})
                elif self.command == "POST" and action == "reveal":
                    path = self.library.verified_item_path(self.library.get_item(item_id))
                    if sys.platform != "darwin":
                        raise ValueError("在文件夹中显示目前适用于 macOS。")
                    subprocess.run(["/usr/bin/open", "-R", str(path)], check=True, timeout=10)
                    self.send_json({"ok": True})
                else:
                    self.send_json({"error": "不支持这个操作。"}, 405)
        except Exception as exc:
            self.fail(exc)

    do_POST = mutate
    do_PATCH = mutate
    do_DELETE = mutate


class ContentHandler(BaseHandler):
    def do_GET(self):
        if not self.valid_host():
            return
        try:
            route = urlsplit(self.path).path
            match = re.fullmatch(r"/files/([A-Za-z0-9_-]+)/(.+)", route)
            if not match:
                self.send_json({"error": "页面不存在，请从 HTML 资料库打开。"}, 404)
                return
            capability, encoded_relative = match.groups()
            relative = unquote(encoded_relative)
            if "\x00" in relative or "\\" in relative or relative.startswith("/") or ".." in Path(relative).parts:
                raise PermissionError("path")
            with self.library.lock:
                row = self.library.db.execute("SELECT path FROM roots WHERE capability=?", (capability,)).fetchone()
            if not row:
                raise KeyError("资料库中已移除这个文件夹。")
            root = Path(row["path"])
            if not root_available(root):
                raise PermissionError("root changed")
            path = (root / relative).resolve()
            if not contained(path, root) or contained(path, self.library.data_dir) or contained(path, APP_DIR):
                raise PermissionError("path")
            if any(part.startswith(".") for part in Path(relative).parts) or blocked_resource_path(path, root):
                raise PermissionError("path")
            if not path.is_file():
                raise FileNotFoundError("文件不存在。")
            if path.suffix.lower() not in RESOURCE_EXTENSIONS:
                raise PermissionError("unsupported resource")
            content_type = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
            if content_type.startswith("text/") or content_type in ("application/javascript", "application/json"):
                content_type += "; charset=utf-8"
            data = path.read_bytes()
            if path.suffix.lower() in (".html", ".htm", ".css"):
                rewriter = RootResourceRewriter(root, path, capability, self.library.data_dir)
                try:
                    source = data.decode("utf-8-sig")
                    if path.suffix.lower() == ".css":
                        data = rewriter.rewrite_css(source).encode("utf-8")
                    else:
                        rewriter.feed(source)
                        rewriter.close()
                        data = "".join(rewriter.parts).encode("utf-8")
                except (UnicodeDecodeError, ValueError):
                    pass
            self.send_bytes(data, content_type, headers={
                "Cross-Origin-Opener-Policy": "same-origin",
                "Content-Security-Policy": "frame-ancestors 'none'",
            })
        except Exception as exc:
            self.fail(exc)

    def do_HEAD(self):
        self.do_GET()

    def do_OPTIONS(self):
        if self.valid_host():
            self.send_json({"error": "不支持此请求。"}, 403)


def main(argv=None):
    parser = argparse.ArgumentParser(description="本地 HTML 资料库：自动收录、全文搜索、收藏与预览")
    parser.add_argument("--port", type=int, default=18765, help="资料库入口端口（默认 18765）")
    parser.add_argument("--content-port", type=int, default=18766, help="隔离的 HTML 内容端口（默认 18766）")
    parser.add_argument("--data-dir", default=str(APP_DIR / ".data"), help="本地索引与设置目录")
    parser.add_argument("--root", action="append", help="添加扫描文件夹；可重复使用")
    parser.add_argument("--interval", type=float, default=10, help="自动扫描间隔秒数（默认 10）")
    args = parser.parse_args(argv)
    if args.port == args.content_port or not (1 <= args.port <= 65535 and 1 <= args.content_port <= 65535):
        parser.error("入口端口与内容端口必须不同，且在 1–65535 之间。")
    library = Library(args.data_dir, args.port, args.content_port, args.root, args.interval)
    servers = []
    try:
        servers.append(LibraryHTTPServer(("127.0.0.1", args.port), AppHandler, library))
        servers.append(LibraryHTTPServer(("127.0.0.1", args.content_port), ContentHandler, library))
    except OSError as exc:
        for server in servers:
            server.server_close()
        library.close()
        print("启动失败，端口可能已被占用：%s" % str(exc), file=sys.stderr)
        return 1
    shutting_down = threading.Event()

    def stop(signum=None, frame=None):
        shutting_down.set()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    library.start()
    for server in servers:
        threading.Thread(target=server.serve_forever, daemon=True).start()
    print("HTML 资料库已启动：%s" % library.app_origin, flush=True)
    print("每 %s 秒自动扫描；关闭网页后继续收录。" % ("%g" % library.interval), flush=True)
    try:
        while not shutting_down.wait(1):
            pass
    except KeyboardInterrupt:
        pass
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()
        library.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
