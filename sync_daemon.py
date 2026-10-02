"""Demon de synchronisation du contenu du kiosque hors-ligne.

Le demon telecharge un manifeste distant (JSON), recupere chaque
fichier, verifie son hash SHA-256, tout d'abord dans un dossier de
staging, puis remplace les fichiers de maniere atomique. Le marquage
``.version`` est ecrit en dernier : il sert de point de commit pour
les lecteurs (nginx, navigateur).

Aucune dependance externe : bibliotheca standard uniquement.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import logging
import os
import random
import shutil
import signal
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Dict, List, Optional, Tuple

LOGGER = logging.getLogger("kiosk-sync")
VERSION_MARKER = ".version"
MAX_MANIFEST_BYTES = 1_048_576
MAX_FILE_BYTES = 512 * 1_048_576
USER_AGENT = "pi-kiosk-sync/1.0"


class SyncError(Exception):
    """Erreur de synchronisation bloquante pour le cycle courant."""


def sha256_of(path: Path) -> str:
    """Calcule le hash SHA-256 d'un fichier par paquets."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def safe_relative_path(raw: str) -> Path:
    """Valide un chemin de manifeste, refuse toute evasion."""
    if not raw or raw.startswith("/") or "\\" in raw:
        raise SyncError("chemin de manifeste invalide: %r" % raw)
    candidate = Path(raw)
    parts = candidate.parts
    if any(part == ".." or part == "" for part in parts):
        raise SyncError("chemin de manifeste invalide: %r" % raw)
    if candidate.is_absolute():
        raise SyncError("chemin de manifeste invalide: %r" % raw)
    return candidate


def fetch_json(url: str, timeout: float) -> Dict[str, object]:
    """Telecharge un document JSON avec limite de taille."""
    request = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as reply:
            raw = reply.read(MAX_MANIFEST_BYTES + 1)
    except (urllib.error.URLError, OSError, ValueError) as error:
        raise SyncError("manifeste inaccessible: %s" % error) from error
    if len(raw) > MAX_MANIFEST_BYTES:
        raise SyncError("manifeste trop volumineux")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as error:
        raise SyncError("manifeste JSON illisible") from error
    if not isinstance(data, dict):
        raise SyncError("manifeste JSON de forme invalide")
    return data


def download_file(
    url: str,
    target: Path,
    expected_sha: str,
    timeout: float,
) -> None:
    """Telecharge un fichier dans ``target`` et verifie son hash."""
    request = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as reply:
            declared = reply.headers.get("Content-Length")
            if declared and int(declared) > MAX_FILE_BYTES:
                raise SyncError("fichier trop volumineux: %s" % url)
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("wb") as handle:
                written = 0
                while True:
                    chunk = reply.read(64 * 1024)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > MAX_FILE_BYTES:
                        raise SyncError(
                            "fichier trop volumineux: %s" % url
                        )
                    handle.write(chunk)
    except SyncError:
        raise
    except (urllib.error.URLError, OSError, ValueError) as error:
        raise SyncError("telechargement impossible: %s" % error) from error
    actual = sha256_of(target)
    if actual.lower() != expected_sha.lower():
        raise SyncError(
            "hash invalide pour %s (attendu %s...)"
            % (target.name, expected_sha[:12])
        )


class SyncManager:
    """Synchronise un dossier de contenu depuis un manifeste HTTP."""

    def __init__(
        self,
        base_url: str,
        content_dir: Path,
        staging_dir: Path,
        state_file: Path,
        manifest_path: str = "/api/v1/content/latest",
        interval: float = 60.0,
        timeout: float = 30.0,
        force: bool = False,
    ) -> None:
        """Prepare le demon sans effectuer de requete."""
        self.base_url = base_url.rstrip("/")
        self.content_dir = content_dir
        self.staging_dir = staging_dir
        self.state_file = state_file
        self.manifest_path = manifest_path
        self.interval = float(interval)
        self.timeout = float(timeout)
        self.force = bool(force)
        self.stop_event = threading.Event()

    @property
    def manifest_url(self) -> str:
        """URL complete du manifeste."""
        return "%s%s" % (self.base_url, self.manifest_path)

    def load_state(self) -> Dict[str, object]:
        """Lit l'etat local, ou un etat vide."""
        try:
            data = json.loads(self.state_file.read_text("utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def save_state(self, version: str, count: int) -> None:
        """Ecrit l'etat de facon atomique."""
        payload = {
            "version": version,
            "files": count,
            "synced_at": time.strftime(
                "%Y-%m-%dT%H:%M:%SZ",
                time.gmtime(),
            ),
        }
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_file.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(payload, ensure_ascii=True, indent=2),
            encoding="utf-8",
        )
        tmp.replace(self.state_file)

    def sync_once(self) -> Dict[str, object]:
        """Execute un cycle complet, renvoie un rapport."""
        report: Dict[str, object] = {"status": "error", "version": ""}
        try:
            manifest = fetch_json(self.manifest_url, self.timeout)
        except SyncError as error:
            report["error"] = str(error)
            return report

        version = str(manifest.get("version", "")).strip()
        files = manifest.get("files")
        if not version or not isinstance(files, list):
            report["error"] = "manifeste incomplet (version/files)"
            return report
        report["version"] = version

        state = self.load_state()
        if not self.force and state.get("version") == version:
            report["status"] = "skipped"
            return report

        entries: List[Tuple[Path, str, str]] = []
        try:
            for item in files:
                if not isinstance(item, dict):
                    raise SyncError("entree de manifeste invalide")
                rel = safe_relative_path(str(item.get("path", "")))
                sha = str(item.get("sha256", "")).strip()
                if len(sha) != 64:
                    raise SyncError(
                        "hash absent pour %s" % rel
                    )
                url = str(item.get("url", "")).strip()
                if not url:
                    quoted = urllib.parse.quote(str(rel))
                    url = "%s/%s" % (self.base_url, quoted)
                elif not url.startswith(("http://", "https://")):
                    if not url.startswith("/"):
                        url = "/" + url
                    url = "%s%s" % (self.base_url, url)
                entries.append((rel, sha, url))
        except SyncError as error:
            report["error"] = str(error)
            return report

        if self.staging_dir.exists():
            shutil.rmtree(self.staging_dir)
        self.staging_dir.mkdir(parents=True, exist_ok=True)

        try:
            for rel, sha, url in entries:
                download_file(
                    url,
                    self.staging_dir / rel,
                    sha,
                    self.timeout,
                )
        except SyncError as error:
            report["error"] = str(error)
            shutil.rmtree(self.staging_dir, ignore_errors=True)
            return report

        self.content_dir.mkdir(parents=True, exist_ok=True)
        replaced = 0
        try:
            for rel, _sha, _url in entries:
                source = self.staging_dir / rel
                target = self.content_dir / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                os.replace(source, target)
                replaced += 1
            marker = self.content_dir / VERSION_MARKER
            tmp_marker = self.content_dir / (VERSION_MARKER + ".tmp")
            tmp_marker.write_text(version + "\n", encoding="utf-8")
            os.replace(tmp_marker, marker)
        except OSError as error:
            report["error"] = "activation impossible: %s" % error
            report["files"] = replaced
            return report

        shutil.rmtree(self.staging_dir, ignore_errors=True)
        self.save_state(version, replaced)
        report["status"] = "synced"
        report["files"] = replaced
        return report

    def run_forever(self) -> None:
        """Boucle principale avec jitter et arret propre."""
        LOGGER.info(
            "demarrage: %s -> %s (toutes les %.0fs)",
            self.manifest_url,
            self.content_dir,
            self.interval,
        )
        while not self.stop_event.is_set():
            report = self.sync_once()
            status = report.get("status")
            if status == "synced":
                LOGGER.info(
                    "contenu synchronise: version=%s fichiers=%s",
                    report.get("version"),
                    report.get("files"),
                )
            elif status == "skipped":
                LOGGER.debug(
                    "version deja synchrone: %s",
                    report.get("version"),
                )
            else:
                LOGGER.warning(
                    "echec de synchronisation: %s",
                    report.get("error"),
                )
            delay = self.interval * random.uniform(0.9, 1.1)
            self.stop_event.wait(delay)
        LOGGER.info("arret demande, demon termine")

    def request_stop(self) -> None:
        """Demande l'arret de la boucle."""
        self.stop_event.set()


class SingleInstance:
    """Verrou fichier empechant deux demons concurrents."""

    def __init__(self, path: Path) -> None:
        """Prepare le verrou."""
        self.path = path
        self._handle = None

    def acquire(self) -> bool:
        """Prend le verrou, faux si un autre demon tourne."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            return False
        handle.seek(0)
        handle.truncate()
        handle.write(str(os.getpid()))
        handle.flush()
        self._handle = handle
        return True

    def release(self) -> None:
        """Libere le verrou de facon idempotente."""
        if self._handle is not None:
            try:
                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
                self._handle.close()
            finally:
                self._handle = None


def build_parser() -> argparse.ArgumentParser:
    """Construit la ligne de commande du demon."""
    parser = argparse.ArgumentParser(
        prog="sync_daemon",
        description="Synchronise le contenu du kiosque hors-ligne.",
    )
    parser.add_argument(
        "--base-url",
        default=os.environ.get("SYNC_BASE_URL", "http://127.0.0.1:8080"),
        help="URL d'origine du contenu (defaut: %(default)s)",
    )
    parser.add_argument(
        "--content-dir",
        default=os.environ.get("CONTENT_DIR", "./content"),
        help="dossier de contenu servi au navigateur (defaut: %(default)s)",
    )
    parser.add_argument(
        "--staging-dir",
        default=os.environ.get("STAGING_DIR", "./.staging"),
        help="dossier de preparation (defaut: %(default)s)",
    )
    parser.add_argument(
        "--state-file",
        default=os.environ.get("STATE_FILE", "./.sync-state.json"),
        help="fichier d'etat (defaut: %(default)s)",
    )
    parser.add_argument(
        "--lock-file",
        default=os.environ.get("LOCK_FILE", "./.sync.lock"),
        help="verrou d'instance (defaut: %(default)s)",
    )
    parser.add_argument(
        "--manifest",
        default=os.environ.get("SYNC_MANIFEST", "/api/v1/content/latest"),
        help="chemin du manifeste (defaut: %(default)s)",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=float(os.environ.get("SYNC_INTERVAL", "60")),
        help="periode entre deux cycles (defaut: %(default)s)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=float(os.environ.get("SYNC_TIMEOUT", "30")),
        help="timeout HTTP en secondes (defaut: %(default)s)",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="un seul cycle puis sortie",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="resynchronise meme si la version est deja presente",
    )
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="valide la chaine complete avec un serveur local",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="journalisation de niveau debug",
    )
    return parser


def self_test() -> int:
    """Exerce manifeste, telechargement, verification et activation."""
    import tempfile

    failures = 0
    with tempfile.TemporaryDirectory(prefix="kiosk-selftest-") as tmp:
        root = Path(tmp)
        origin = root / "origin"
        (origin / "assets").mkdir(parents=True)
        (origin / "index.html").write_text(
            "<h1>kiosque</h1>\n",
            encoding="utf-8",
        )
        (origin / "assets" / "logo.txt").write_text(
            "logo\n",
            encoding="utf-8",
        )
        entries = []
        for rel in ("index.html", "assets/logo.txt"):
            digest = sha256_of(origin / rel)
            entries.append(
                {"path": rel, "sha256": digest, "url": "/%s" % rel}
            )
        manifest = {"version": "v-selftest", "files": entries}
        (origin / "api" / "v1" / "content").mkdir(
            parents=True,
            exist_ok=True,
        )
        (origin / "api" / "v1" / "content" / "latest").write_text(
            json.dumps(manifest),
            encoding="utf-8",
        )

        def handler(*args, **kwargs):
            return SimpleHTTPRequestHandler(
                *args, directory=str(origin), **kwargs
            )
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = "http://127.0.0.1:%d" % server.server_address[1]

        try:
            manager = SyncManager(
                base_url=base,
                content_dir=root / "content",
                staging_dir=root / "staging",
                state_file=root / "state.json",
                interval=1.0,
                timeout=5.0,
            )
            first = manager.sync_once()
            if first.get("status") != "synced":
                LOGGER.error("self-test 1er cycle: %s", first)
                failures += 1
            if not (root / "content" / "index.html").is_file():
                LOGGER.error("self-test: index.html absent")
                failures += 1
            marker = (root / "content" / VERSION_MARKER)
            stored = ""
            if marker.is_file():
                stored = marker.read_text(encoding="utf-8").strip()
            if stored != "v-selftest":
                LOGGER.error("self-test: marqueur .version invalide")
                failures += 1

            second = manager.sync_once()
            if second.get("status") != "skipped":
                LOGGER.error("self-test 2e cycle: %s", second)
                failures += 1

            evil = SyncManager(
                base_url=base,
                content_dir=root / "content2",
                staging_dir=root / "staging2",
                state_file=root / "state2.json",
                interval=1.0,
                timeout=5.0,
                force=True,
            )
            (origin / "api" / "v1" / "content" / "latest").write_text(
                json.dumps(
                    {
                        "version": "v-evil",
                        "files": [
                            {
                                "path": "../evil.txt",
                                "sha256": "0" * 64,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            third = evil.sync_once()
            if third.get("status") != "error":
                LOGGER.error("self-test anti-evasion: %s", third)
                failures += 1
            if (root / "evil.txt").exists() or (
                root / "content2"
            ).exists():
                LOGGER.error("self-test: evasion detectee")
                failures += 1
        finally:
            server.shutdown()
            server.server_close()

    if failures:
        LOGGER.error("self-test: %d echec(s)", failures)
        return 1
    LOGGER.info("self-test: OK")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    """Point d'entree du demon."""
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format=(
            "%(asctime)s %(levelname)s %(name)s %(message)s"
        ),
        datefmt="%Y-%m-%dT%H:%M:%S",
    )

    if args.self_test:
        return self_test()

    if args.interval < 1.0:
        LOGGER.error("intervalle minimal: 1s")
        return 2

    lock = SingleInstance(Path(args.lock_file))
    if not lock.acquire():
        LOGGER.info("un autre demon utilise deja %s", args.lock_file)
        return 0

    manager = SyncManager(
        base_url=args.base_url,
        content_dir=Path(args.content_dir),
        staging_dir=Path(args.staging_dir),
        state_file=Path(args.state_file),
        manifest_path=args.manifest,
        interval=args.interval,
        timeout=args.timeout,
        force=args.force,
    )

    def handle_signal(signum, frame) -> None:
        """Demande un arret propre sur signal systeme."""
        del signum, frame
        manager.request_stop()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    try:
        if args.once:
            report = manager.sync_once()
            LOGGER.info("cycle unique: %s", report)
            return 0 if report.get("status") != "error" else 1
        manager.run_forever()
        return 0
    finally:
        lock.release()


if __name__ == "__main__":
    sys.exit(main())
