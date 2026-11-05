"""THIS robot's own Pokamind credential: the per-robot token.

One credential per robot is an Ed25519 key pair. The robot holds the 32-byte private
SEED; Pokamind stores only the public key, so adding, rotating or revoking a robot
never requires touching the robot. What an operator pastes is one string::

    pkr1.<robot_id>.<seed>

* ``robot_id`` -- ``^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$``: the robot's Pokamind identity.
* ``seed``     -- base64url, NO padding, of exactly 32 bytes (43 characters).

One token signs both planes, separated by the JWT ``aud``: the 45-second control-plane
assertion (:meth:`RobotToken.control_assertion`) and the partner-SSO login token
(``hostapp/jwt_sso.mint_robot_sso_jwt``). Both carry the header
``{"alg": "EdDSA", "typ": "JWT", "kid": <robot_id>}``.

What may be shown, and where:

* the FULL token is never logged, never put in a URL and never written anywhere but
  the token file (``RobotToken.__repr__`` and ``__str__`` are the masked form, so an
  f-string or a ``%r`` in a log line cannot leak it by accident);
* the **masked** form ``pkr1.<robot_id>.…<last 4 of the seed>`` is the only form a log
  line or a status page carries;
* the **fingerprint** ``base64url(sha256(raw public key))[:16]`` is not a secret and is
  what the robot and Pokamind compare to agree they mean the same key.

Storage: a file, not ``.env`` -- ``EMORI_POKAMIND_TOKEN_FILE`` (default
``secrets/pokamind_robot_token``; a relative path is relative to the INSTALL ROOT,
:func:`install_root`, i.e. ``/opt/emori/secrets/pokamind_robot_token`` on the robot,
whatever directory a tool is started from; directory 0700, file 0600).
Resolution: the file if present and non-empty, else ``POKAMIND_ROBOT_TOKEN`` (dev and
tests), else none -- in which case the robot has no Pokamind credential at all: the
kiosk serves demo sessions only, add-on modes stay off and readycheck FAILs
``pokamind_token_missing``. Like every other credential on the robot the token binds
at PROCESS START (:func:`configured`): replacing it means restarting the hostapp.

Parsing, masking and storage are standard library; signing and the fingerprint need
``cryptography`` (and PyJWT for the JWT), imported lazily so a box without the hostapp
extras can still parse and mask a token.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import re
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional

log = logging.getLogger("emori.robot_token")

TOKEN_PREFIX = "pkr1"
TOKEN_FILE_ENV = "EMORI_POKAMIND_TOKEN_FILE"
TOKEN_ENV = "POKAMIND_ROBOT_TOKEN"
DEFAULT_TOKEN_FILE = "secrets/pokamind_robot_token"

CONTROL_AUDIENCE = "pokamind-robot-control"
SSO_AUDIENCE = "pokamind-partner-sso"
CONTROL_ASSERTION_TTL_S = 45
# The robot mints 600 s; the server enforces a maximum assertion lifetime of 900 s.
SSO_TTL_S = 600
SSO_MAX_TTL_S = 900

ROBOT_ID_RE = re.compile(r"[a-z0-9][a-z0-9-]{1,61}[a-z0-9]\Z")
_SEED_RE = re.compile(r"[A-Za-z0-9_-]{43}\Z")
_SEED_BYTES = 32
# A token is ~110 characters. Anything this large in the token file is not a token,
# and reading it whole would let a stray copy-paste of a log into the file cost RAM.
_MAX_TOKEN_FILE_BYTES = 4096
_SLUG_UNSAFE = re.compile(r"[^a-z0-9._-]+")
_MASK_ELLIPSIS = "…"

# Stable, content-free reasons a token was refused. Safe in a log line, a JSON body
# and a readycheck code: none of them can carry any part of the value.
TOKEN_ERROR_CODES = frozenset({
    "token_empty",
    "token_malformed",
    "token_version_unsupported",
    "robot_id_invalid",
    "seed_invalid",
    "token_file_unreadable",
    "token_file_too_large",
})


class TokenError(ValueError):
    """A refused token. ``code`` names the reason; the message never quotes the value."""

    def __init__(self, code: str) -> None:
        self.code = code if code in TOKEN_ERROR_CODES else "token_malformed"
        super().__init__(self.code)


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def robot_slug(value: str) -> str:
    """``EMORI_ROBOT_ID`` -> the slug identity.py derives the robot's learner from.

    The one normalisation both sides of the robot_id cross-check use: a token's
    robot_id is already a slug, so ``robot_slug(token.robot_id) == token.robot_id``,
    and an ``EMORI_ROBOT_ID`` of ``Ward 3`` compares equal to a token for ``ward-3``.
    """

    slug = _SLUG_UNSAFE.sub("-", (value or "").strip().lower()).strip("-._")
    return re.sub(r"-{2,}", "-", slug)


def fingerprint_of(public_key: bytes) -> str:
    """``base64url(sha256(raw 32-byte public key))[:16]`` -- display + cross-check only."""

    return _b64url(hashlib.sha256(public_key).digest())[:16]


class RobotToken:
    """A parsed per-robot token. Holds the seed; prints only its masked form."""

    __slots__ = ("robot_id", "_seed", "_public_key")

    def __init__(self, robot_id: str, seed: bytes) -> None:
        if type(robot_id) is not str or ROBOT_ID_RE.fullmatch(robot_id) is None:
            raise TokenError("robot_id_invalid")
        if type(seed) is not bytes or len(seed) != _SEED_BYTES:
            raise TokenError("seed_invalid")
        self.robot_id = robot_id
        self._seed = seed
        self._public_key: Optional[bytes] = None

    # -- what may be shown --------------------------------------------------------
    @property
    def masked(self) -> str:
        return f"{TOKEN_PREFIX}.{self.robot_id}.{_MASK_ELLIPSIS}{_b64url(self._seed)[-4:]}"

    def __repr__(self) -> str:
        return f"RobotToken({self.masked})"

    __str__ = __repr__

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, RobotToken):
            return NotImplemented
        return self.robot_id == other.robot_id and hmac.compare_digest(self._seed, other._seed)

    def __hash__(self) -> int:
        return hash((RobotToken, self.robot_id))

    def reveal(self) -> str:
        """The full token string. Callers: the token file writer and the PIN-gated reveal."""

        return f"{TOKEN_PREFIX}.{self.robot_id}.{_b64url(self._seed)}"

    # -- the key ------------------------------------------------------------------
    def _private_key(self):
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (  # noqa: PLC0415
            Ed25519PrivateKey,
        )

        return Ed25519PrivateKey.from_private_bytes(self._seed)

    @property
    def public_key(self) -> bytes:
        if self._public_key is None:
            from cryptography.hazmat.primitives import serialization  # noqa: PLC0415

            self._public_key = self._private_key().public_key().public_bytes(
                serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        return self._public_key

    @property
    def public_key_b64u(self) -> str:
        return _b64url(self.public_key)

    @property
    def fingerprint(self) -> str:
        return fingerprint_of(self.public_key)

    # -- signing ------------------------------------------------------------------
    def sign(self, claims: Mapping[str, Any]) -> str:
        """An EdDSA JWT over ``claims`` with ``kid`` = this robot's id.

        The caller owns the claim set; this only signs. PyJWT writes ``alg`` and
        ``typ`` itself, so the header is exactly ``{alg: EdDSA, typ: JWT, kid}``.
        """

        import jwt  # noqa: PLC0415 - PyJWT, in the hostapp extra

        encoded = jwt.encode(dict(claims), self._private_key(), algorithm="EdDSA",
                             headers={"kid": self.robot_id})
        return encoded.decode("utf-8") if isinstance(encoded, bytes) else encoded

    def control_assertion(self, now: Optional[int] = None) -> str:
        """The control-plane bearer assertion: the fixed claim set, signed EdDSA.

        ``sub`` (= robot_id), ``aud`` (``pokamind-robot-control``), ``iat``, ``exp`` =
        iat + 45 and ``jti``, under header ``kid`` = robot_id, which tells Pokamind
        which robot's public key verifies it.
        """

        if now is None:
            issued = int(time.time())
        elif type(now) is int and now >= 0:
            issued = now
        else:
            raise ValueError("invalid_assertion_time")
        return self.sign({
            "sub": self.robot_id,
            "aud": CONTROL_AUDIENCE,
            "iat": issued,
            "exp": issued + CONTROL_ASSERTION_TTL_S,
            "jti": str(uuid.uuid4()),
        })


def parse_token(value: object) -> RobotToken:
    """``pkr1.<robot_id>.<seed>`` -> :class:`RobotToken`, or :class:`TokenError`.

    Surrounding whitespace is forgiven (a file's trailing newline, a paste with a
    space), anything inside is not. The seed must be the CANONICAL base64url of 32
    bytes: a 43-character string has two spare bits, and accepting a non-zero pair
    would let two different strings mean one key -- a fingerprint would then no
    longer identify exactly one pasted value.
    """

    if type(value) is not str:
        raise TokenError("token_malformed")
    text = value.strip()
    if not text:
        raise TokenError("token_empty")
    if any(character.isspace() for character in text) or not text.isascii():
        raise TokenError("token_malformed")
    parts = text.split(".")
    if len(parts) != 3:
        raise TokenError("token_malformed")
    prefix, robot_id, seed_text = parts
    if prefix != TOKEN_PREFIX:
        raise TokenError("token_version_unsupported"
                         if re.fullmatch(r"pkr[0-9]+", prefix) else "token_malformed")
    if ROBOT_ID_RE.fullmatch(robot_id) is None:
        raise TokenError("robot_id_invalid")
    if _SEED_RE.fullmatch(seed_text) is None:
        raise TokenError("seed_invalid")
    try:
        seed = _b64url_decode(seed_text)
    except (ValueError, TypeError):
        raise TokenError("seed_invalid") from None
    if len(seed) != _SEED_BYTES or _b64url(seed) != seed_text:
        raise TokenError("seed_invalid")
    return RobotToken(robot_id, seed)


def mask(value: object) -> str:
    """The masked form of anything that might be a token -- never more of it.

    For a value that does not parse there is nothing safe to show beyond the fact
    that it was not a token, so the answer is a fixed placeholder.
    """

    try:
        return parse_token(value).masked
    except TokenError:
        return f"{TOKEN_PREFIX}.?.{_MASK_ELLIPSIS}"


def generate(robot_id: str) -> RobotToken:
    """A fresh token for ``robot_id`` (tests, and a bench without the issuing CLI).

    Production tokens are issued only by Pokamind, which is the only place a public
    key gets registered; a token minted here is unknown to Pokamind until its public
    key is registered there.
    """

    return RobotToken(robot_id, os.urandom(_SEED_BYTES))


# --------------------------------------------------------------------------- storage
def _is_file(path: Path) -> bool:
    try:
        return path.is_file()
    except OSError:  # a directory we may not look into is not where the install is
        return False


def install_root() -> Optional[Path]:
    """The directory a relative path in this install's configuration is relative to.

    ``/opt/emori`` on a robot. In order:

    1. the first directory holding a ``.env``, walking up from where
       :mod:`emori_adapter.config` lives -- the walk config itself makes to find the
       ``.env`` it loads (``EMORI_POKAMIND_TOKEN_FILE`` comes from that file); from
       this package when config was never imported;
    2. a source checkout's root (``<root>/src/emori_adapter`` with a
       ``pyproject.toml``), which is what an editable install points at;

    else ``None``, and the caller falls back to the working directory.

    The working directory is the LAST resort on purpose: resolved against it, any tool
    run outside ``/opt/emori`` would silently find no token and act as a robot without
    a Pokamind credential.
    And never python-dotenv's ``find_dotenv()``, which walks from the working directory
    under ``python -c``, a REPL or a debugger. Every path here is ``abspath``-ed, like
    config's, so the two walks cannot disagree about a symlinked install.

    config is read from ``sys.modules``, never imported: importing it loads ``.env``
    into the environment, and parsing a token must not do that as a side effect.
    """

    config = sys.modules.get("emori_adapter.config")
    start = getattr(config, "__file__", None) if config is not None else None
    if not isinstance(start, str) or not start:
        start = __file__
    package = os.path.dirname(os.path.abspath(start))
    directory = package
    while True:
        if _is_file(Path(directory) / ".env"):
            return Path(directory)
        parent = os.path.dirname(directory)
        if parent == directory:
            break
        directory = parent
    checkout = os.path.dirname(os.path.dirname(package))
    if os.path.basename(os.path.dirname(package)) == "src" \
            and _is_file(Path(checkout) / "pyproject.toml"):
        return Path(checkout)
    return None


def token_file_path(environ: Optional[Mapping[str, str]] = None,
                    cwd: Optional[os.PathLike | str] = None) -> Path:
    """Where this robot keeps its token: ``EMORI_POKAMIND_TOKEN_FILE`` or the default.

    A relative path resolves against ``cwd`` when a caller passes one (tests), else
    against :func:`install_root`, and against the working directory only when there
    is no install root at all.
    """

    env = os.environ if environ is None else environ
    raw = (env.get(TOKEN_FILE_ENV, "") or "").strip() or DEFAULT_TOKEN_FILE
    path = Path(raw).expanduser()
    if not path.is_absolute():
        base = cwd if cwd is not None else (install_root() or os.getcwd())
        path = Path(base) / path
    return path


_warned_lock = threading.Lock()
_warned_missing: set[str] = set()


def _warn_missing_once(path: Path) -> None:
    """An EXPLICITLY configured token file that is not there: say so, once per path.

    The default path being absent is reported by readycheck (``pokamind_token_missing``);
    a path somebody wrote into ``.env`` being absent is a typo or a missing copy, and
    without this line the only symptom is a robot that serves demo sessions only.
    """

    key = str(path)
    with _warned_lock:
        if key in _warned_missing:
            return
        _warned_missing.add(key)
    log.warning("%s points at %s, which does not exist: this process has no Pokamind "
                "token file (it falls back to %s if set, else it has no Pokamind "
                "credential)", TOKEN_FILE_ENV, key, TOKEN_ENV)


@dataclass(frozen=True)
class TokenLoad:
    """What the token resolution found. ``token`` is set only when it parsed.

    ``source`` is where the value came from (``file``/``env``) or ``none``; ``error``
    is a :data:`TOKEN_ERROR_CODES` code when a value was present but unusable.
    ``insecure_permissions`` is a POSIX token file readable by anyone but its owner.
    """

    token: Optional[RobotToken] = None
    source: str = "none"
    path: str = ""
    error: str = ""
    insecure_permissions: bool = False

    @property
    def state(self) -> str:
        if self.token is not None:
            return "configured"
        return "invalid" if self.error else "none"

    def describe(self) -> dict:
        """Everything about the load that is safe to show. Never the token."""

        token = self.token
        return {
            "state": self.state,
            "source": self.source,
            "path": self.path,
            "error": self.error or None,
            "robot_id": token.robot_id if token is not None else None,
            "masked": token.masked if token is not None else None,
            "insecure_permissions": self.insecure_permissions,
        }


def _insecure(mode: int) -> bool:
    # Windows reports synthetic permission bits (0o666 for any writable file), so
    # the question only has an answer on POSIX.
    return os.name == "posix" and bool(mode & 0o077)


def load_token(environ: Optional[Mapping[str, str]] = None,
               cwd: Optional[os.PathLike | str] = None) -> TokenLoad:
    """Resolve the token: the file if present and non-empty, else the env, else none.

    Never raises. A file that exists but cannot be read, or holds something that is
    not a token, is reported as ``error`` with ``source="file"`` -- and does NOT fall
    through to the env: a broken file is an operator problem to surface, not one to
    paper over with a value from somewhere else.
    """

    env = os.environ if environ is None else environ
    path = token_file_path(env, cwd)
    shown = str(path)
    try:
        status = os.stat(path)
    except FileNotFoundError:
        status = None
        if (env.get(TOKEN_FILE_ENV, "") or "").strip():
            _warn_missing_once(path)
    except OSError:
        return TokenLoad(source="file", path=shown, error="token_file_unreadable")
    if status is not None:
        if status.st_size > _MAX_TOKEN_FILE_BYTES:
            return TokenLoad(source="file", path=shown, error="token_file_too_large")
        try:
            with open(path, "rb") as handle:
                raw = handle.read(_MAX_TOKEN_FILE_BYTES + 1)
        except OSError:
            return TokenLoad(source="file", path=shown, error="token_file_unreadable")
        if len(raw) > _MAX_TOKEN_FILE_BYTES:
            return TokenLoad(source="file", path=shown, error="token_file_too_large")
        try:
            text = raw.decode("ascii")
        except UnicodeDecodeError:
            return TokenLoad(source="file", path=shown, error="token_malformed",
                             insecure_permissions=_insecure(status.st_mode))
        if text.strip():
            try:
                token = parse_token(text)
            except TokenError as e:
                return TokenLoad(source="file", path=shown, error=e.code,
                                 insecure_permissions=_insecure(status.st_mode))
            return TokenLoad(token=token, source="file", path=shown,
                             insecure_permissions=_insecure(status.st_mode))
    value = (env.get(TOKEN_ENV, "") or "").strip()
    if value:
        try:
            return TokenLoad(token=parse_token(value), source="env", path=shown)
        except TokenError as e:
            return TokenLoad(source="env", path=shown, error=e.code)
    return TokenLoad(path=shown)


def _chmod(path: os.PathLike | str, mode: int) -> None:
    """Best effort: on Windows ``os.chmod`` only toggles the read-only bit."""

    try:
        os.chmod(path, mode)
    except OSError:
        pass


# Beside the token file: the token a replacement replaced, so a wrong one can be rolled
# back (deploy/robot/install-token.sh keeps it the same way; docs/deploy-a-new-robot.md,
# "Replace, rotate, revoke").
PREVIOUS_SUFFIX = ".prev"


def _write_private(directory: Path, target: Path, data: bytes) -> None:
    """``data`` into ``target`` atomically, 0600 from its first byte."""

    fd, temporary = tempfile.mkstemp(prefix=".pokamind_robot_token.", dir=str(directory))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        _chmod(temporary, 0o600)
        os.replace(temporary, target)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    _chmod(target, 0o600)


def save_token(token: RobotToken, path: Optional[os.PathLike | str] = None) -> Path:
    """Write ``token`` to the token file atomically: directory 0700, file 0600.

    Same-directory temp file + ``fsync`` + ``os.replace``, so a power cut leaves
    either the old token or the new one, never half of one. ``mkstemp`` creates the
    temp file 0600 from the start, so the value is never on disk with wider
    permissions even for an instant. A DIFFERENT token already in the file is kept
    first, the same way, as ``<file>.prev``: a replacement can be rolled back. Raises
    ``OSError`` when the write fails.
    """

    if not isinstance(token, RobotToken):
        raise TypeError("save_token needs a parsed RobotToken")
    target = Path(path) if path is not None else token_file_path()
    directory = target.parent
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    _chmod(directory, 0o700)
    data = (token.reveal() + "\n").encode("ascii")
    try:
        previous = target.read_bytes()[:_MAX_TOKEN_FILE_BYTES]
    except OSError:
        previous = b""
    if previous.strip() and previous.strip() != data.strip():
        _write_private(directory, target.with_name(target.name + PREVIOUS_SUFFIX), previous)
    _write_private(directory, target, data)
    if os.name == "posix":
        # The rename itself must survive a power cut too.
        try:
            dir_fd = os.open(str(directory), os.O_RDONLY)
        except OSError:
            dir_fd = -1
        if dir_fd >= 0:
            try:
                os.fsync(dir_fd)
            except OSError:
                pass
            finally:
                os.close(dir_fd)
    return target


# ------------------------------------------------------------- the process binding
_configured_lock = threading.Lock()
_configured: Optional[TokenLoad] = None


def configured() -> TokenLoad:
    """The token THIS process runs with, resolved once (env binds at process start).

    Every reader in one process -- the SSO minting, any add-on client, the
    anonymous learner identity -- must agree on one token, and a token saved by the
    admin dashboard takes effect through the hostapp restart that follows, exactly
    like an edit to ``.env``. :func:`reset_configured` exists for tests.
    """

    global _configured
    with _configured_lock:
        if _configured is None:
            _configured = load_token()
        return _configured


def reset_configured() -> None:
    global _configured
    with _configured_lock:
        _configured = None


def robot_id_conflict(token: Optional[RobotToken], env_robot_id: str) -> bool:
    """``EMORI_ROBOT_ID`` is set AND names a different robot than the token.

    Unset is not a conflict (the id is then derived from the token). The token wins
    for every Pokamind call either way; this only decides whether to say so loudly.
    """

    if token is None:
        return False
    configured_id = robot_slug(env_robot_id or "")
    return bool(configured_id) and configured_id != token.robot_id
