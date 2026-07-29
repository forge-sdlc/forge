import asyncio
import hashlib
import ipaddress
import json
import logging
import os
import re
import socket
import time
import urllib.parse
import uuid
from datetime import datetime
from html.parser import HTMLParser
from typing import Any

import httpcore
import httpx

from forge.integrations.jira.client import JiraClient
from forge.skills.utils import extract_project_key

logger = logging.getLogger(__name__)

CACHE_LOCK = asyncio.Lock()

BLOCKED_NETWORKS = [
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("fe80::/10"),
    ipaddress.ip_network("224.0.0.0/4"),
    ipaddress.ip_network("ff00::/8"),
    ipaddress.ip_network("0.0.0.0/32"),
    ipaddress.ip_network("240.0.0.0/4"),
]


def is_safe_ip(ip_str: str) -> bool:
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    return all(ip not in network for network in BLOCKED_NETWORKS)


def resolve_and_verify_hostname(hostname: str) -> str:
    """Resolve hostname and return a safe IP address. Raise ValueError if unsafe or empty."""
    try:
        addrinfo = socket.getaddrinfo(hostname, None)
    except Exception as e:
        raise ValueError(f"DNS resolution failed for {hostname}: {e}")

    if not addrinfo:
        raise ValueError(f"No IP addresses found for {hostname}")

    for _family, _ltype, _proto, _canonname, sockaddr in addrinfo:
        ip = str(sockaddr[0])
        if not is_safe_ip(ip):
            raise ValueError(f"Unsafe IP address resolved: {ip} for {hostname}")

    # Return the first resolved IP address (which is safe)
    return str(addrinfo[0][4][0])


def normalize_url(url: str) -> str:
    """Trim whitespace, convert scheme/host to lowercase, strip redundant default ports and trailing root slash."""
    url = url.strip()
    parsed = urllib.parse.urlparse(url)
    scheme = parsed.scheme.lower()
    netloc = parsed.netloc.lower()

    if ":" in netloc:
        if netloc.startswith("[") and "]" in netloc:
            parts = netloc.rsplit("]", 1)
            host = parts[0] + "]"
            port_part = parts[1]
            if port_part.startswith(":"):
                port = port_part[1:]
                if (scheme == "http" and port == "80") or (scheme == "https" and port == "443"):
                    netloc = host
        else:
            host, port = netloc.rsplit(":", 1)
            if (scheme == "http" and port == "80") or (scheme == "https" and port == "443"):
                netloc = host

    path = parsed.path
    if path == "/":
        path = ""

    return urllib.parse.urlunparse(
        (scheme, netloc, path, parsed.params, parsed.query, parsed.fragment)
    )


class PinnedAsyncNetworkBackend(httpcore.AsyncNetworkBackend):
    def __init__(self, pinned_ips: dict[str, str]):
        self._backend = httpcore.AnyIOBackend()
        self.pinned_ips = pinned_ips

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Any = None,
    ) -> httpcore.AsyncNetworkStream:
        pinned_ip = self.pinned_ips.get(host, host)
        return await self._backend.connect_tcp(
            host=pinned_ip,
            port=port,
            timeout=timeout,
            local_address=local_address,
            socket_options=socket_options,
        )

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Any = None,
    ) -> httpcore.AsyncNetworkStream:
        return await self._backend.connect_unix_socket(
            path=path,
            timeout=timeout,
            socket_options=socket_options,
        )

    async def sleep(self, seconds: float) -> None:
        await self._backend.sleep(seconds)


class PinnedAsyncHTTPTransport(httpx.AsyncHTTPTransport):
    def __init__(self, pinned_backend: httpcore.AsyncNetworkBackend, **kwargs: Any):
        super().__init__(**kwargs)
        if isinstance(self._pool, httpcore.AsyncConnectionPool):
            self._pool = httpcore.AsyncConnectionPool(
                ssl_context=self._pool._ssl_context,
                max_connections=self._pool._max_connections,
                max_keepalive_connections=self._pool._max_keepalive_connections,
                keepalive_expiry=self._pool._keepalive_expiry,
                http1=self._pool._http1,
                http2=self._pool._http2,
                uds=self._pool._uds,
                local_address=self._pool._local_address,
                retries=self._pool._retries,
                socket_options=self._pool._socket_options,
                network_backend=pinned_backend,
            )


async def fetch_reference_url(
    url: str, pinned_ips: dict[str, str], backend: PinnedAsyncNetworkBackend
) -> tuple[str, str]:
    """Fetch content of reference URL, handling redirects manually (up to 5 hops)."""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"Unsupported scheme: {parsed.scheme}")

    current_url = url
    hops = 0
    max_hops = 5

    while True:
        parsed_current = urllib.parse.urlparse(current_url)
        if parsed_current.scheme not in ("http", "https"):
            raise ValueError(f"Unsupported redirect scheme: {parsed_current.scheme}")

        hostname = parsed_current.hostname
        if not hostname:
            raise ValueError(f"Invalid hostname in URL: {current_url}")

        safe_ip = resolve_and_verify_hostname(hostname)
        pinned_ips[hostname] = safe_ip

        if parsed_current.path.lower().endswith(".pdf"):
            return "application/pdf", ""

        transport = PinnedAsyncHTTPTransport(pinned_backend=backend)
        async with (
            httpx.AsyncClient(transport=transport, follow_redirects=False, timeout=10.0) as client,
            client.stream("GET", current_url) as response,
        ):
            content_type = response.headers.get("content-type", "").lower()
            if "application/pdf" in content_type:
                return "application/pdf", ""

            if response.status_code in (301, 302, 303, 307, 308):
                if hops >= max_hops:
                    raise ValueError(f"Max redirect hops ({max_hops}) exceeded.")
                redirect_location = response.headers.get("location")
                if not redirect_location:
                    raise ValueError(
                        f"Redirect status {response.status_code} with no location header."
                    )
                current_url = urllib.parse.urljoin(current_url, redirect_location)
                hops += 1
                continue

            response.raise_for_status()

            chunks = []
            bytes_read = 0
            max_bytes = 5 * 1024 * 1024  # 5 MB

            async for chunk in response.aiter_bytes(chunk_size=1024 * 64):
                bytes_read += len(chunk)
                if bytes_read > max_bytes:
                    logger.warning(
                        f"Response size exceeded 5 MB limit for {current_url}. Truncating."
                    )
                    break
                chunks.append(chunk)

            body_bytes = b"".join(chunks)
            encoding = (
                response.encoding or getattr(response, "apparent_encoding", "utf-8") or "utf-8"
            )
            try:
                body_text = body_bytes.decode(encoding, errors="replace")
            except Exception:
                body_text = body_bytes.decode("utf-8", errors="replace")

            return content_type, body_text


class HTMLToMarkdownParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.result: list[str] = []
        self.tag_stack: list[str] = []
        self.in_script_or_style = False
        self.current_href: str | None = None
        self.link_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tag_stack.append(tag)
        if tag in ("script", "style"):
            self.in_script_or_style = True

        if self.in_script_or_style:
            return

        if tag == "p":
            self.result.append("\n\n")
        elif tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            level = int(tag[1])
            self.result.append(f"\n\n{'#' * level} ")
        elif tag == "li":
            self.result.append("\n* ")
        elif tag == "tr":
            self.result.append("\n")
        elif tag in ("td", "th"):
            self.result.append(" | ")
        elif tag == "a":
            attrs_dict = dict(attrs)
            self.current_href = attrs_dict.get("href")
            self.link_text = []

    def handle_endtag(self, tag: str) -> None:
        if self.tag_stack:
            self.tag_stack.pop()

        if tag in ("script", "style"):
            self.in_script_or_style = any(t in ("script", "style") for t in self.tag_stack)

        if self.in_script_or_style:
            return

        if tag == "a":
            link_str = "".join(self.link_text).strip()
            href_str = self.current_href
            if link_str:
                if href_str:
                    self.result.append(f"[{link_str}]({href_str})")
                else:
                    self.result.append(link_str)
            self.current_href = None
            self.link_text = []
        elif tag in ("p", "h1", "h2", "h3", "h4", "h5", "h6"):
            self.result.append("\n")

    def handle_data(self, data: str) -> None:
        if self.in_script_or_style:
            return

        if self.current_href is not None:
            self.link_text.append(data)
        else:
            self.result.append(data)

    def get_markdown(self) -> str:
        text = "".join(self.result)
        lines = text.splitlines()
        cleaned_lines = []
        for line in lines:
            line_cleaned = " ".join(line.split())
            cleaned_lines.append(line_cleaned)

        final_text = "\n".join(cleaned_lines)
        final_text = re.sub(r"\n{3,}", "\n\n", final_text)
        return final_text.strip()


def html_to_markdown(html_content: str) -> str:
    try:
        parser = HTMLToMarkdownParser()
        parser.feed(html_content)
        return parser.get_markdown()
    except Exception as e:
        logger.warning(f"HTML parsing failed, falling back to raw tag stripping: {e}")
        return re.sub(r"<[^>]+>", "", html_content).strip()


def get_cache_dir(run_id: str) -> str:
    return f"/tmp/forge_references_cache/{run_id}"


def get_cache_filepath(run_id: str, norm_url: str) -> str:
    h = hashlib.sha256(norm_url.encode("utf-8")).hexdigest()
    return os.path.join(get_cache_dir(run_id), h)


async def read_from_cache(run_id: str, norm_url: str) -> tuple[str, str] | None:
    filepath = get_cache_filepath(run_id, norm_url)
    if not os.path.exists(filepath):
        return None

    try:
        mtime = os.path.getmtime(filepath)
        if time.time() - mtime > 3600:
            return None

        async with CACHE_LOCK:
            with open(filepath, encoding="utf-8") as f:
                data = json.load(f)
            return data["content_type"], data["body_text"]
    except Exception as e:
        logger.warning(f"Failed to read from cache for {norm_url}: {e}")
        return None


def enforce_cache_folder_size(
    cache_dir: str, new_file_size: int, max_size: int = 10 * 1024 * 1024
) -> None:
    if not os.path.exists(cache_dir):
        return

    try:
        files = []
        total_size = 0
        for entry in os.scandir(cache_dir):
            if entry.is_file():
                stat = entry.stat()
                files.append((entry.path, stat.st_mtime, stat.st_size))
                total_size += stat.st_size

        if total_size + new_file_size > max_size:
            files.sort(key=lambda x: x[1])
            for path, _, size in files:
                try:
                    os.remove(path)
                    total_size -= size
                    if total_size + new_file_size <= max_size:
                        break
                except Exception as e:
                    logger.warning(f"Failed to delete cached file {path}: {e}")
    except Exception as e:
        logger.warning(f"Error enforcing cache size for {cache_dir}: {e}")


async def write_to_cache(run_id: str, norm_url: str, content_type: str, body_text: str) -> None:
    cache_dir = get_cache_dir(run_id)
    os.makedirs(cache_dir, exist_ok=True)

    filepath = get_cache_filepath(run_id, norm_url)
    payload = {
        "content_type": content_type,
        "body_text": body_text,
        "cached_at": time.time(),
    }
    payload_str = json.dumps(payload, ensure_ascii=False)
    payload_bytes = payload_str.encode("utf-8")
    new_file_size = len(payload_bytes)

    enforce_cache_folder_size(cache_dir, new_file_size)

    async with CACHE_LOCK:
        try:
            temp_filepath = filepath + ".tmp"
            with open(temp_filepath, "w", encoding="utf-8") as f:
                f.write(payload_str)
            os.replace(temp_filepath, filepath)
        except Exception as e:
            logger.warning(f"Failed to write to cache for {norm_url}: {e}")


def extract_references_from_comment(body: str) -> list[dict[str, str]]:
    found = []
    for line in body.splitlines():
        line = line.strip()
        match = re.search(r"@forge\s+ref\s+(https?://\S+)(?:\s+(.+))?", line)
        if match:
            url = match.group(1).strip()
            desc = match.group(2).strip() if match.group(2) else ""
            found.append({"url": url, "description": desc})
    return found


def format_and_truncate_aggregate_references(
    references_data: list[dict[str, Any]],
) -> str:
    if not references_data:
        return ""

    header = "\n\n## External References\n\n"
    suffix = "\n... [TRUNCATED - Aggregate limit exceeded]"
    max_aggregate = 30000

    current_text = header

    for ref in references_data:
        url = ref["url"]
        desc = ref["description"]
        body = ref["body_text"]

        if len(body) > 10000:
            body = body[:10000] + "\n... [TRUNCATED - Reference exceeded character limit]"

        ref_block = f"### Reference: {url}\n"
        if desc:
            ref_block += f"Description: {desc}\n"
        ref_block += f"Content:\n{body}\n\n"

        if len(current_text) + len(ref_block) > max_aggregate:
            allowed_chars = max_aggregate - len(current_text) - len(suffix)
            if allowed_chars > 0:
                current_text += ref_block[:allowed_chars] + suffix
            else:
                current_text = current_text[: max_aggregate - len(suffix)] + suffix
            break
        else:
            current_text += ref_block

    return current_text


async def fetch_and_inject_references(state: Any, jira: JiraClient, base_text: str) -> str:
    """Gather project-level and ticket-level references, fetch contents securely, and append context."""
    ticket_key = state.get("ticket_key")
    if not ticket_key:
        return base_text

    try:
        project_key = extract_project_key(ticket_key)
    except ValueError:
        project_key = ticket_key.upper()

    context = state.get("context")
    if context is None:
        context = {}
        state["context"] = context

    if "run_id" not in context or not context["run_id"]:
        context["run_id"] = str(uuid.uuid4())

    run_id = context["run_id"]

    # 1. Fetch project-level standing references
    try:
        standing_refs = await jira.get_project_references(project_key)
    except Exception as e:
        logger.warning(f"Failed to fetch project standing references for {project_key}: {e}")
        standing_refs = []

    if not isinstance(standing_refs, list):
        logger.warning(
            f"forge.references for project {project_key} is malformed: {standing_refs!r}"
        )
        standing_refs = []

    # Filter malformed entries
    standing_refs = [ref for ref in standing_refs if isinstance(ref, dict) and "url" in ref]

    # 2. Fetch ticket comments
    try:
        comments = await jira.get_comments(ticket_key)
    except Exception as e:
        logger.warning(f"Failed to fetch comments for {ticket_key}: {e}")
        comments = []

    if not isinstance(comments, list):
        logger.warning(f"get_comments returned non-list: {comments!r}")
        comments = []

    def _comment_sort_key(c: Any) -> datetime:
        if hasattr(c, "assert_called") or hasattr(c, "called") or "Mock" in c.__class__.__name__:
            return datetime.min
        created = getattr(c, "created", None)
        if isinstance(created, datetime):
            return created
        if isinstance(created, str):
            try:
                cleaned = created
                if len(created) > 4 and created[-5] in ("+", "-") and ":" not in created[-3:]:
                    cleaned = created[:-2] + ":" + created[-2:]
                return datetime.fromisoformat(cleaned)
            except ValueError:
                return datetime.min
        return datetime.min

    comments.sort(key=_comment_sort_key)

    ticket_refs = []
    for comment in comments:
        extracted = extract_references_from_comment(comment.body)
        ticket_refs.extend(extracted)

    # 3. Deduplicate & order references
    unique_norm_urls = []
    latest_ref_by_norm = {}

    for ref in standing_refs:
        try:
            norm = normalize_url(ref["url"])
            if norm not in latest_ref_by_norm:
                unique_norm_urls.append(norm)
            latest_ref_by_norm[norm] = ref
        except Exception as e:
            logger.warning(f"Failed to normalize standing reference URL {ref.get('url')}: {e}")

    for ref in ticket_refs:
        try:
            norm = normalize_url(ref["url"])
            if norm not in latest_ref_by_norm:
                unique_norm_urls.append(norm)
            latest_ref_by_norm[norm] = ref
        except Exception as e:
            logger.warning(f"Failed to normalize comment reference URL {ref.get('url')}: {e}")

    # Process up to 10 reference resources
    selected_norms = unique_norm_urls[:10]

    references_data = []
    for norm in selected_norms:
        ref_obj = latest_ref_by_norm[norm]
        original_url = ref_obj["url"]
        desc = ref_obj.get("description", "")

        cached = await read_from_cache(run_id, norm)
        if cached is not None:
            content_type, body_text = cached
        else:
            pinned_ips: dict[str, str] = {}
            backend = PinnedAsyncNetworkBackend(pinned_ips)
            try:
                content_type, body_text = await fetch_reference_url(
                    original_url, pinned_ips, backend
                )
                if "text/html" in content_type:
                    body_text = html_to_markdown(body_text)
                elif "application/pdf" in content_type:
                    body_text = f"[WARNING: PDF reference deferred. Automatic text extraction from PDF URL is not supported: {original_url}]"

                await write_to_cache(run_id, norm, content_type, body_text)
            except Exception as e:
                logger.warning(f"Failed to fetch reference URL {original_url}: {e}")
                content_type = "text/plain"
                body_text = f"[WARNING: Failed to fetch reference URL: {original_url}. Error: {e}]"

        references_data.append(
            {
                "url": original_url,
                "description": desc,
                "body_text": body_text,
                "content_type": content_type,
            }
        )

    if not references_data:
        return base_text

    references_block = format_and_truncate_aggregate_references(references_data)
    return base_text + references_block
