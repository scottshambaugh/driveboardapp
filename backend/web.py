import concurrent.futures
import copy
import glob
import gzip
import hashlib
import json
import multiprocessing
import os
import shutil
import socketserver
import sys
import tempfile
import threading
import time
import traceback
import uuid
import webbrowser
import wsgiref.simple_server

import bottle
import config as config_module
import driveboard
import jobimport
from config import conf, conf_defaults, userconfigurable, write_config_fields
from jobimport import pathoptimizer

__author__ = "Stefan Hechenberger <stefan@nortd.com>"

DEBUG = False
bottle.BaseRequest.MEMFILE_MAX = 1024 * 1024 * 100  # max 100Mb files
time_reconnect_last = 0
_reconnect_lock = threading.Lock()
jog_target_last = None  # last jog target commanded, in offset coordinates

if conf["mill_mode"]:
    frontend_path = "frontend_mill"
    print("INFO: loading mill mode frontend")
else:
    frontend_path = "frontend"


def checkuser(user, pw):
    """Check login credentials, used by auth_basic decorator."""
    return bool(user in conf["users"] and conf["users"][user] == pw)


def machine_busy_plugin(callback):
    """Answer 409 when something asks the machine to move while a job is being
    queued onto it, rather than letting it surface as a server error."""

    def wrapper(*args, **kwargs):
        try:
            return callback(*args, **kwargs)
        except driveboard.MachineBusy as e:
            raise bottle.HTTPResponse(str(e), 409) from e

    return wrapper


bottle.default_app().install(machine_busy_plugin)


def checkserial(func):
    """Decorator to call function only when machine connected."""

    def _decorator(*args, **kwargs):
        if driveboard.connected():
            return func(*args, **kwargs)
        else:
            raise bottle.HTTPResponse("No machine.", 400)

    return _decorator


### STATIC FILES


def _frontend_file(filename, *subdirs):
    """Serve a frontend asset, revalidated on every request.

    Without an explicit Cache-Control a browser is free to guess how long the
    response stays fresh, and the usual guess of a tenth of the file's age lets
    a client keep running frontend code that is weeks older than the backend it
    talks to. no-cache still allows the cached copy to be reused, it just has to
    be revalidated first, which costs one 304 per asset.
    """
    root = os.path.join(conf["rootdir"], frontend_path, *subdirs)
    response = bottle.static_file(filename, root=root)
    response.set_header("Cache-Control", "no-cache")
    return response


@bottle.route("/")
def default_handler():
    return _frontend_file("app.html")


@bottle.route("/<file>")
def static_bin_handler(file):
    return _frontend_file(file)


@bottle.route("/css/<path:path>")
def static_css_handler(path):
    return _frontend_file(path, "css")


@bottle.route("/fonts/<path:path>")
def static_font_handler(path):
    return _frontend_file(path, "fonts")


@bottle.route("/js/<path:path>")
def static_js_handler(path):
    return _frontend_file(path, "js")


@bottle.route("/img/<path:path>")
def static_img_handler(path):
    return _frontend_file(path, "img")


@bottle.route("/favicon.ico")
def favicon_handler():
    return _frontend_file("favicon.ico", "img")


@bottle.route("/temp", method="POST")
@bottle.auth_basic(checkuser)
def temp():
    """Create temp file for downloading."""
    load_request = json.loads(bottle.request.forms.get("load_request"))
    job = load_request.get("job")  # always a string
    fp = tempfile.NamedTemporaryFile(mode="w", delete=False)
    filename = fp.name
    with fp:
        fp.write(job)
        fp.close()
    print(job)
    print("file stashed: " + os.path.basename(filename))
    # return os.path.basename(filename)
    return json.dumps(os.path.basename(filename))


@bottle.route("/download/<filename>/<dlname>")
@bottle.auth_basic(checkuser)
def download(filename, dlname):
    print("requesting: " + filename)
    return bottle.static_file(filename, root=tempfile.gettempdir(), download=dlname)


### LOW-LEVEL


@bottle.route("/config")
@bottle.route("/config/<key>/<value:path>")
@bottle.auth_basic(checkuser)
def config(key=None, value=None):
    if not key or not value:
        confcopy = copy.deepcopy(conf)
        del confcopy["users"]
        # Build defaults for editable fields only
        defaults = {k: conf_defaults[k] for k in userconfigurable}
        return json.dumps(
            {
                "values": confcopy,
                "editable": userconfigurable,
                "defaults": defaults,
                "configpath": config_module.configpath,
            }
        )
    else:
        if key in userconfigurable:
            if value == "_default_":
                value = conf_defaults[key]
            else:
                try:
                    value = json.loads(value)
                except ValueError:
                    pass
            conf[key] = value
            write_config_fields({key: value})
            return json.dumps({"status": "ok", "message": "Written to config file."})
        else:
            return json.dumps({"status": "error", "message": "Not a user-configurable key."})


@bottle.route("/confserial")
@bottle.route("/confserial/<port>")
@bottle.auth_basic(checkuser)
def confserial(port=None):
    """Write serial port to configuration file."""
    if port:
        conf["serial_port"] = port
        write_config_fields({"serial_port": port})
        return "Serial port written to config file."
    else:
        return conf["serial_port"]


@bottle.route("/status")
@bottle.auth_basic(checkuser)
def status():
    global time_reconnect_last
    # while disconnected, retry the reconnect at most every 5s so the frequent
    # status polls don't hammer it. Concurrent polls would otherwise both pass
    # the check and reconnect twice over, and reconnect() swaps the serial loop
    # out from under whatever else is using it.
    if not driveboard.connected() and (time.time() - time_reconnect_last) > 5.0:
        with _reconnect_lock:
            if (time.time() - time_reconnect_last) > 5.0:
                time_reconnect_last = time.time()
                driveboard.reconnect()
    return json.dumps(driveboard.status())


@bottle.route("/homing")
@bottle.auth_basic(checkuser)
@checkserial
def homing():
    driveboard.homing()
    return "{}"


@bottle.route("/feedrate/<val:float>")
@bottle.auth_basic(checkuser)
@checkserial
def feedrate(val):
    driveboard.feedrate(val)
    return "{}"


@bottle.route("/intensity/<val:float>")
@bottle.auth_basic(checkuser)
@checkserial
def intensity(val):
    driveboard.intensity(val)
    return "{}"


@bottle.route("/relative")
@bottle.auth_basic(checkuser)
@checkserial
def relative():
    driveboard.relative()
    return "{}"


@bottle.route("/absolute")
@bottle.auth_basic(checkuser)
@checkserial
def absolute():
    driveboard.absolute()
    return "{}"


@bottle.route("/retract")
@bottle.auth_basic(checkuser)
@checkserial
def retract():
    with driveboard.machine_operation():
        driveboard.intensity(0)
        driveboard.feedrate(conf["seekrate"])
        driveboard.supermove(z=0)
        driveboard.supermove(x=0, y=0)
    return "{}"


def _jog_target(dx, dy, dz):
    """Absolute target for a jog step, held inside the work area.

    Positions and move() targets share the same offset coordinates, in which
    the work area spans [-offset, workspace - offset] on each axis. Z is only
    bounded when the config gives it a non-zero work area, matching
    target_in_workarea, since a machine with no Z axis still has to be able to
    jog the focus.
    """
    global jog_target_last
    stats = driveboard.status()
    pos = stats.get("pos") or [0.0, 0.0, 0.0]
    offset = stats.get("offset") or [0.0, 0.0, 0.0]
    # An idle machine has drained both buffers, so its reported position has
    # settled and is what the next move starts from. While it is still moving
    # the position lags behind, and a burst of jogs has to accumulate onto the
    # last target commanded or every step of the burst clamps against the same
    # stale position.
    if stats.get("ready") or jog_target_last is None:
        base = pos
    else:
        base = jog_target_last
    requested = [base[i] + d for i, d in enumerate((dx, dy, dz))]
    target = list(requested)
    for i in range(3):
        if i == 2 and not conf["workspace"][2]:
            continue
        target[i] = max(-offset[i], min(target[i], conf["workspace"][i] - offset[i]))
    jog_target_last = target
    return target, target != requested


@bottle.route("/jog/<x:float>/<y:float>/<z:float>")
@bottle.auth_basic(checkuser)
@checkserial
def jog(x, y, z):
    global jog_target_last
    with driveboard.machine_operation():
        driveboard.intensity(0)
        driveboard.feedrate(conf["seekrate"])
        if not conf["jog_soft_limits"]:
            jog_target_last = None  # a relative jog leaves the tracked target behind
            driveboard.relative()
            driveboard.move(x, y, z)
            driveboard.absolute()
            return "{}"
        # Sent absolute, so the commanded target is inside the work area by
        # construction even if the position it was derived from was stale.
        target, clamped = _jog_target(x, y, z)
        driveboard.absolute()
        driveboard.move(*target)
    return json.dumps({"clamped": clamped})


@bottle.route("/move/<x:float>/<y:float>/<z:float>")
@bottle.auth_basic(checkuser)
@checkserial
def move(x, y, z):
    with driveboard.machine_operation():
        if not driveboard.target_in_workarea(x, y, z):
            bottle.abort(400, "move target outside work area")
        driveboard.intensity(0)
        driveboard.move(x, y, z)
    return "{}"


@bottle.route("/movex/<x:float>")
@bottle.auth_basic(checkuser)
@checkserial
def movex(x):
    with driveboard.machine_operation():
        if not driveboard.target_in_workarea(x=x):
            bottle.abort(400, "move target outside work area")
        driveboard.intensity(0)
        driveboard.move(x=x)
    return "{}"


@bottle.route("/movey/<y:float>")
@bottle.auth_basic(checkuser)
@checkserial
def movey(y):
    with driveboard.machine_operation():
        if not driveboard.target_in_workarea(y=y):
            bottle.abort(400, "move target outside work area")
        driveboard.intensity(0)
        driveboard.move(y=y)
    return "{}"


@bottle.route("/movez/<z:float>")
@bottle.auth_basic(checkuser)
@checkserial
def movez(z):
    with driveboard.machine_operation():
        if not driveboard.target_in_workarea(z=z):
            bottle.abort(400, "move target outside work area")
        driveboard.intensity(0)
        driveboard.move(z=z)
    return "{}"


@bottle.route("/supermove/<x:float>/<y:float>/<z:float>")
@bottle.auth_basic(checkuser)
@checkserial
def supermove(x, y, z):
    if not driveboard.target_in_workarea(x, y, z, machine_coords=True):
        bottle.abort(400, "move target outside work area")
    driveboard.supermove(x, y, z)
    return "{}"


@bottle.route("/supermovex/<x:float>")
@bottle.auth_basic(checkuser)
@checkserial
def supermovex(x):
    if not driveboard.target_in_workarea(x=x, machine_coords=True):
        bottle.abort(400, "move target outside work area")
    driveboard.supermove(x=x)
    return "{}"


@bottle.route("/supermovey/<y:float>")
@bottle.auth_basic(checkuser)
@checkserial
def supermovey(y):
    if not driveboard.target_in_workarea(y=y, machine_coords=True):
        bottle.abort(400, "move target outside work area")
    driveboard.supermove(y=y)
    return "{}"


@bottle.route("/supermovez/<z:float>")
@bottle.auth_basic(checkuser)
@checkserial
def supermovez(z):
    if not driveboard.target_in_workarea(z=z, machine_coords=True):
        bottle.abort(400, "move target outside work area")
    driveboard.supermove(z=z)
    return "{}"


@bottle.route("/air_on")
@bottle.auth_basic(checkuser)
@checkserial
def air_on():
    driveboard.air_on()
    return "{}"


@bottle.route("/air_off")
@bottle.auth_basic(checkuser)
@checkserial
def air_off():
    driveboard.air_off()
    return "{}"


@bottle.route("/aux_on")
@bottle.auth_basic(checkuser)
@checkserial
def aux_on():
    driveboard.aux_on()
    return "{}"


@bottle.route("/aux_off")
@bottle.auth_basic(checkuser)
@checkserial
def aux_off():
    driveboard.aux_off()
    return "{}"


@bottle.route("/pulse")
@bottle.auth_basic(checkuser)
@checkserial
def pulse():
    driveboard.pulse()
    return "{}"


@bottle.route("/offset/<x:float>/<y:float>/<z:float>")
@bottle.auth_basic(checkuser)
@checkserial
def offset(x, y, z):
    if not driveboard.status()["ready"]:
        raise bottle.HTTPResponse("Machine not ready.", 400)
    driveboard.offset(x, y, z)
    return "{}"


@bottle.route("/offsetx/<x:float>")
@bottle.auth_basic(checkuser)
@checkserial
def offsetx(x):
    if not driveboard.status()["ready"]:
        raise bottle.HTTPResponse("Machine not ready.", 400)
    driveboard.offset(x=x)
    return "{}"


@bottle.route("/offsety/<y:float>")
@bottle.auth_basic(checkuser)
@checkserial
def offsety(y):
    if not driveboard.status()["ready"]:
        raise bottle.HTTPResponse("Machine not ready.", 400)
    driveboard.offset(y=y)
    return "{}"


@bottle.route("/offsetz/<z:float>")
@bottle.auth_basic(checkuser)
@checkserial
def offsetz(z):
    if not driveboard.status()["ready"]:
        raise bottle.HTTPResponse("Machine not ready.", 400)
    driveboard.offset(z=z)
    return "{}"


@bottle.route("/absoffset/<x:float>/<y:float>/<z:float>")
@bottle.auth_basic(checkuser)
@checkserial
def absoffset(x, y, z):
    if not driveboard.status()["ready"]:
        raise bottle.HTTPResponse("Machine not ready.", 400)
    driveboard.absoffset(x, y, z)
    return "{}"


### JOBS QUEUE


_queue_lock = threading.RLock()


def _get_sorted(globpattern, library=False, stripext=False):
    with _queue_lock:
        if library:
            directory = os.path.join(conf["rootdir"], "library")
            paths = list(filter(os.path.isfile, glob.glob(os.path.join(directory, globpattern))))
            files = [os.path.basename(path) for path in paths]
            files.sort()
        else:
            directory = conf["stordir"]
            paths = list(filter(os.path.isfile, glob.glob(os.path.join(directory, globpattern))))
            files = [os.path.basename(path) for path in paths]
            files.sort(key=lambda x: os.path.getmtime(os.path.join(directory, x)))
        if stripext:
            for i in range(len(files)):
                if files[i].endswith(".dba"):
                    files[i] = files[i][:-4]
                elif files[i].endswith(".dba.starred"):
                    files[i] = files[i][:-12]
    return files


def _get(jobname, library=False):
    # get job as sting
    if library:
        jobpath = os.path.join(conf["rootdir"], "library", jobname.strip("/\\"))
    else:
        jobpath = os.path.join(conf["stordir"], jobname.strip("/\\"))
    with _queue_lock:
        if os.path.exists(jobpath + ".dba"):
            jobpath = jobpath + ".dba"
        elif os.path.exists(jobpath + ".dba.starred"):
            jobpath = jobpath + ".dba.starred"
        else:
            raise bottle.HTTPResponse("No such file.", 400)
        with open(jobpath) as fp:
            job = fp.read()
    return job


def _get_path(jobname, library=False):
    if library:
        jobpath = os.path.join(conf["rootdir"], "library", jobname.strip("/\\"))
    else:
        jobpath = os.path.join(conf["stordir"], jobname.strip("/\\"))
    if os.path.exists(jobpath + ".dba"):
        return jobpath + ".dba"
    elif os.path.exists(jobpath + ".dba.starred"):
        return jobpath + ".dba.starred"
    else:
        raise bottle.HTTPResponse("No such file.", 400)


def _exists(jobname):
    namepath = os.path.join(conf["stordir"], jobname.strip("/\\"))
    if os.path.exists(namepath + ".dba") or os.path.exists(namepath + ".dba.starred"):
        raise bottle.HTTPResponse("File name exists.", 400)


def _clear(limit=None):
    with _queue_lock:
        files = _get_sorted("*.dba")
        if type(limit) is not int and limit is not None:
            raise ValueError
        for filename in files:
            if type(limit) is int and limit <= 0:
                break
            filename = os.path.join(conf["stordir"], filename)
            os.remove(filename)
            print("file deleted: " + filename)
            if type(limit) is int:
                limit -= 1


def _add(job, name):
    # add job (dba string)
    # overwrites file if already exists, use _unique_name(name) to avoid
    with _queue_lock:
        namepath = os.path.join(conf["stordir"], name.strip("/\\") + ".dba")
        fd, tmppath = tempfile.mkstemp(prefix=".job-", suffix=".tmp", dir=conf["stordir"])
        try:
            with os.fdopen(fd, "w") as fp:
                fp.write(job)
                fp.flush()
                os.fsync(fp.fileno())
            os.replace(tmppath, namepath)
        except Exception:
            try:
                os.remove(tmppath)
            except FileNotFoundError:
                pass
            raise
        print("file saved: " + namepath)
        # delete excessive job files
        num_to_del = len(_get_sorted("*.dba")) - conf["max_jobs_in_list"]
        _clear(num_to_del)


def _unique_name(jobname):
    files = _get_sorted("*.dba*", stripext=True)
    if jobname in files:
        for i in range(2, 999):
            altname = f"{jobname}_{i}"
            if altname in files:
                continue
            else:
                jobname = altname
                break
    return jobname


# Importing a job is seconds of C parsing (expat, PIL) that never releases the
# GIL. Done in this interpreter it starves the serial thread, and the
# controller reads that silence as a lost host and stops itself. A worker
# process keeps the work off this interpreter, leaving the request thread
# blocked on a pipe.
#
# Source and result travel as temp files rather than through the pipe, which
# would pickle and copy tens of megabytes each way, and the worker is kept warm
# between requests, since starting one costs about as much as parsing a
# mid-size file.
CONVERT_TIMEOUT = 600  # seconds, a big raster job legitimately takes minutes
_convert_pool = None
_convert_pool_lock = threading.Lock()


def _convert_worker(in_path, out_path, quick_path, text, optimize, matrix, conf_overrides):
    """Convert a job file into a .dba file, in the worker process.

    With quick_path, the parsed-but-unoptimized job lands there as soon as it
    exists, so the caller can show it while the optimization still runs.

    A spawned worker starts from the config defaults, so the server's values
    come over with the call. Tolerance is passed explicitly because convert
    binds it as a default argument at import time.
    """
    conf.update(conf_overrides)
    with open(in_path, "rb") as fp:
        job = fp.read()
    if text:
        job = job.decode("utf-8")
    if quick_path:
        # gcode is procedural, its order is never optimized (see read_gcode)
        optimize = optimize and jobimport.get_type(job) != "gcode"
        job = jobimport.convert(job, optimize=False, tolerance=conf["tolerance"], matrix=matrix)
        with open(quick_path + ".tmp", "w") as fp:
            fp.write(jobimport.dumps(jobimport.share_image_data(job)))
        os.replace(quick_path + ".tmp", quick_path)  # appear only when complete
        if optimize:
            jobimport.optimize_job(job, conf["tolerance"])
    else:
        job = jobimport.convert(job, optimize=optimize, tolerance=conf["tolerance"], matrix=matrix)
    # placements of one picture share its payload on the way to disk, see
    # jobimport.share_image_data
    with open(out_path, "w") as fp:
        fp.write(jobimport.dumps(jobimport.share_image_data(job)))


def _convert_pool_get():
    """The import worker, started on first use and kept for later requests."""
    global _convert_pool
    if _convert_pool is None:
        ctx = multiprocessing.get_context("spawn")
        _convert_pool = concurrent.futures.ProcessPoolExecutor(max_workers=1, mp_context=ctx)
    return _convert_pool


def _convert_pool_drop():
    """Discard the worker so the next request starts a clean one."""
    global _convert_pool
    with _convert_pool_lock:
        pool, _convert_pool = _convert_pool, None
    if pool is not None:
        for proc in list(pool._processes.values()):
            proc.kill()
        pool.shutdown(wait=False)


# progressive loads still optimizing, {token: {"event", "name", "error"}}
_load_pending = {}
_load_pending_lock = threading.Lock()


def _pending_register(future, out_path, tmpdir):
    """Track a still-optimizing load, finish it on a watcher thread."""
    token = uuid.uuid4().hex
    entry = {"event": threading.Event(), "name": None, "error": None, "stale": False}
    with _load_pending_lock:
        _load_pending[token] = entry

    def watch():
        try:
            future.result(timeout=CONVERT_TIMEOUT)
            with open(out_path) as fp:
                entry["result"] = fp.read()
            # the queue entry gets the optimized job under the same name,
            # unless that name has been written since (running a job saves it
            # back with its passes, and this optimize predates them)
            # Keep the staleness decision and publication indivisible from a
            # request that marks this result stale and saves a replacement.
            with _load_pending_lock:
                if entry["name"] and not entry["stale"]:
                    _add(entry["result"], entry["name"])
        except Exception as e:
            entry["error"] = str(e)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
            entry["event"].set()

    threading.Thread(target=watch, daemon=True).start()
    return token


def _convert_job(job, optimize, matrix, progressive=False):
    """Convert a job to a .dba string, in a worker process where one can be had.

    Returns (dba_string, token): token is None when the string is final, and
    a /load_result handle when it is the quick parse of a progressive load.

    Spawn rather than fork: this process has the serial and server threads
    running, and a forked child can inherit a held stdout or import lock and
    deadlock. Falling back to converting here costs the serial thread its
    timing, not correctness, so it stays a warning rather than an error.
    """
    text = isinstance(job, str)
    tmpdir = tempfile.mkdtemp(prefix="dbimport_")
    quick_path = os.path.join(tmpdir, "job.quick") if progressive else None
    args = (
        os.path.join(tmpdir, "job.in"),
        os.path.join(tmpdir, "job.dba"),
        quick_path,
        text,
        optimize,
        matrix,
        dict(conf),
    )
    try:
        with open(args[0], "wb") as fp:
            fp.write(job.encode("utf-8") if text else job)
        try:
            with _convert_pool_lock:
                future = _convert_pool_get().submit(_convert_worker, *args)
            try:
                if progressive:
                    # hand back the parsed job as soon as it appears, the
                    # optimized one follows via /load_result
                    waited = 0.0
                    while not future.done() and waited < CONVERT_TIMEOUT:
                        if os.path.exists(quick_path):
                            with open(quick_path) as fp:
                                quick = fp.read()
                            token = _pending_register(future, args[1], tmpdir)
                            tmpdir = None  # the watcher owns the cleanup now
                            return quick, token
                        time.sleep(0.05)
                future.result(timeout=CONVERT_TIMEOUT)
            except concurrent.futures.TimeoutError:
                _convert_pool_drop()
                raise bottle.HTTPResponse("Import timed out.", 504) from None
        except (concurrent.futures.BrokenExecutor, OSError, ImportError) as e:
            print(f"WARN: import worker unavailable ({e}), converting in-process")
            _convert_pool_drop()
            _convert_worker(*args)
        with open(args[1]) as fp:
            return fp.read(), None
    finally:
        if tmpdir:
            shutil.rmtree(tmpdir, ignore_errors=True)


@bottle.route("/load", method="POST")
@bottle.auth_basic(checkuser)
def load():
    """Load a dba, svg, dxf, or gcode job.

    Args:
        (Args come in through the POST request.)
        job: Parsed dba or job string (dba, svg, dxf, or gcode), or the marker
             "upload" / "upload_raw" when it comes as a separate file part.
        name: name of the job (string)
        optimize: flag whether to optimize (bool)
        overwrite: flag whether to overwite file if present (bool)
        matrix: alignment matrix to apply to dba (3x3 list of lists of float)
    """
    load_request = json.loads(bottle.request.forms.get("load_request"))
    job = load_request.get("job")  # always a string
    if job in ("upload", "upload_raw"):  # data was passed as a file upload
        upload = bottle.request.files.get("job", None)
        if upload is None:
            raise bottle.HTTPResponse("Invalid request data.", 400)
        if job == "upload":  # gzip compressed
            job = gzip.GzipFile(fileobj=upload.file, mode="rb").read()
        else:  # uncompressed, the browser streams the file as it is on disk
            job = upload.file.read()

    name = load_request.get("name")
    # optimize defaults
    if "optimize" in load_request:
        optimize = load_request["optimize"]
    else:
        optimize = True
    # overwrite defaults
    if "overwrite" in load_request:
        overwrite = load_request["overwrite"]
    else:
        overwrite = False
    # alignment matrix
    if "matrix" in load_request:
        matrix = load_request["matrix"]
    else:
        matrix = None
    # sanity check
    if job is None or name is None:
        raise bottle.HTTPResponse("Invalid request data.", 400)
    progressive = bool(load_request.get("progressive"))
    # A dba posted back with nothing to do to it is already what convert()
    # would hand back, so it goes to the queue as it stands. This is the run
    # button's path, where the job is the one the frontend was given and can
    # be tens of megabytes: converting it would copy it to the worker and
    # back, re-encode every raster, and queue behind whatever that single
    # worker is still busy with, all to arrive at the same bytes.
    if not optimize and matrix is None and not progressive and jobimport.get_type(job) == "dba":
        job, pending = (job.decode("utf-8") if isinstance(job, bytes) else job), None
    else:
        # convert, off this process so the serial thread keeps its timing
        try:
            job, pending = _convert_job(job, optimize, matrix, progressive)  # a .dba string
        except TypeError:
            if DEBUG:
                traceback.print_exc()
            raise bottle.HTTPResponse("Invalid file type.", 400) from None
        except ValueError as e:
            raise bottle.HTTPResponse(str(e), 422) from e

    # an optimize still running against this name would land on top of what is
    # about to be written, so it is told to stand down first
    with _load_pending_lock:
        if not overwrite:
            name = _unique_name(name)
        for entry in _load_pending.values():
            if entry.get("name") == name:
                entry["stale"] = True
        _add(job, name)
    if pending:
        with _load_pending_lock:
            entry = _load_pending[pending]
            entry["name"] = name
            # the watcher may have finished before the name was known
            if entry["event"].is_set() and entry.get("result") and not entry["error"]:
                _add(entry["result"], name)
        return json.dumps({"name": name, "pending": pending})
    return json.dumps(name)


@bottle.route("/load_result/<token>")
@bottle.auth_basic(checkuser)
def load_result(token):
    """Wait for a progressive load's optimization to finish.

    Returns the job name once the queue entry holds the optimized job, so
    the frontend can re-fetch it via /get.
    """
    with _load_pending_lock:
        entry = _load_pending.get(token)
    if entry is None:
        raise bottle.HTTPResponse("Unknown load token.", 404)
    entry["event"].wait(timeout=CONVERT_TIMEOUT)
    with _load_pending_lock:
        _load_pending.pop(token, None)
    if entry["error"]:
        raise bottle.HTTPResponse(entry["error"], 422)
    if not entry["event"].is_set():
        raise bottle.HTTPResponse("Import timed out.", 504)
    return json.dumps({"name": entry["name"]})


@bottle.route("/listing")
@bottle.route("/listing/<kind>")
@bottle.auth_basic(checkuser)
def listing(kind=None):
    """List all queue jobs by name."""
    if kind is None:
        files = _get_sorted("*.dba*", stripext=True)
    elif kind == "starred":
        files = _get_sorted("*.dba.starred", stripext=True)
        print(files)
    elif kind == "unstarred":
        files = _get_sorted("*.dba", stripext=True)
    else:
        raise bottle.HTTPResponse("Invalid kind.", 400)
    return json.dumps(files)


@bottle.route("/get/<jobname>")
@bottle.auth_basic(checkuser)
def get(jobname="woot"):
    """Get a queue job in .dba format."""
    base, name = os.path.split(_get_path(jobname))
    return bottle.static_file(name, root=base, mimetype="application/json")


@bottle.route("/star/<jobname>")
@bottle.auth_basic(checkuser)
def star(jobname):
    """Star a job."""
    with _queue_lock:
        jobpath = _get_path(jobname)
        if jobpath.endswith(".dba"):
            os.rename(jobpath, jobpath + ".starred")
        else:
            raise bottle.HTTPResponse("No such file.", 400)
    return "{}"


@bottle.route("/unstar/<jobname>")
@bottle.auth_basic(checkuser)
def unstar(jobname):
    """Unstar a job."""
    with _queue_lock:
        jobpath = _get_path(jobname)
        if jobpath.endswith(".starred"):
            os.rename(jobpath, jobpath[:-8])
        else:
            raise bottle.HTTPResponse("No such file.", 400)
    return "{}"


@bottle.route("/remove/<jobname>")
@bottle.auth_basic(checkuser)
def remove(jobname):
    """Delete a job."""
    with _queue_lock:
        jobpath = _get_path(jobname)
        os.remove(jobpath)
    print("INFO: file deleted: " + jobpath)
    return "{}"


@bottle.route("/clear")
@bottle.auth_basic(checkuser)
def clear():
    """Clear job list."""
    _clear()
    return "{}"


### LIBRARY


@bottle.route("/listing_library")
@bottle.auth_basic(checkuser)
def listing_library():
    """List all library jobs by name."""
    files = _get_sorted("*.dba", library=True, stripext=True)
    return json.dumps({"files": files, "path": os.path.join(conf["rootdir"], "library")})


@bottle.route("/get_library/<jobname>")
@bottle.auth_basic(checkuser)
def get_library(jobname):
    """Get a library job in .dba format."""
    base, name = os.path.split(_get_path(jobname, library=True))
    return bottle.static_file(name, root=base, mimetype="application/json")


@bottle.route("/load_library/<jobname>")
@bottle.auth_basic(checkuser)
def load_library(jobname):
    """Load a library job into the queue."""
    job = _get(jobname, library=True)
    with _queue_lock:
        jobname = _unique_name(jobname)
        _add(job, jobname)
    return json.dumps(jobname)


### FAVORITES


def _get_presets_path():
    return os.path.join(conf["confdir"], "presets.json")


def _read_presets():
    presets = []
    path = _get_presets_path()
    # load
    if os.path.exists(path):
        with open(path) as fp:
            try:
                presets = json.load(fp)
                for one_preset in presets:
                    # presets saved before pierce_time existed have no pierce
                    one_preset.setdefault("pierce_time", 0.0)
                presets.sort(key=lambda x: x["name"].lower())
            except Exception:
                print("ERROR: failed to read presets file")
    return presets


@bottle.route("/listing_presets")
@bottle.auth_basic(checkuser)
def listing_presets():
    """List all preset settings."""
    presets = _read_presets()

    return json.dumps({"presets": presets, "path": _get_presets_path()})


# the pierce_time-less form is kept so an older client or a saved link still saves
@bottle.route("/save_preset/<name>/<feedrate:float>/<intensity:float>/<pxsize:float>")
@bottle.route(
    "/save_preset/<name>/<feedrate:float>/<intensity:float>/<pxsize:float>/<pierce_time:float>"
)
@bottle.auth_basic(checkuser)
def save_preset(name, feedrate, intensity, pxsize, pierce_time=0.0):
    """Save a preset setting to presets.json. Delete if feedrate==0 && intensity==0"""
    presets = _read_presets()
    try:
        presets_dict = {one_preset["name"].lower(): one_preset for one_preset in presets}
        if name.lower() in presets_dict and int(feedrate) == 0 and int(intensity) == 0:
            del presets_dict[name.lower()]
        elif int(feedrate) != 0 or int(intensity) != 0:
            presets_dict[name.lower()] = {
                "name": name,
                "feedrate": feedrate,
                "intensity": intensity,
                "pxsize": pxsize,
                "pierce_time": pierce_time,
            }
        presets = list(presets_dict.values())
        presets.sort(key=lambda x: x["name"].lower())
        path = os.path.join(conf["confdir"], "presets.json")
        with open(path, "w") as fp:
            json.dump(presets, fp)
    except Exception as e:
        print("ERROR: failed to update presets file")
        print(e)
    return "{}"


### JOB EXECUTION


@bottle.route("/run/<jobname>")
@bottle.auth_basic(checkuser)
@checkserial
def run(jobname):
    """Send job from queue to the machine."""
    job = _get(jobname)
    if not driveboard.status()["ready"]:
        raise bottle.HTTPResponse("Machine not ready.", 400)
    try:
        driveboard.job(json.loads(job))
    except ValueError as e:
        raise bottle.HTTPResponse(str(e), 422) from e
    return "{}"


@bottle.route("/run", method="POST")
@bottle.auth_basic(checkuser)
@checkserial
def run_direct():
    """Run an dba job directly, by-passing the queue.
    Args:
        (Args come in through the POST request.)
        job: Parsed dba job.
    """
    load_request = json.loads(bottle.request.forms.get("load_request"))
    job = load_request.get("job")  # always a string
    # sanity check
    if job is None:
        raise bottle.HTTPResponse("Invalid request data.", 400)
    if not driveboard.status()["ready"]:
        raise bottle.HTTPResponse("Machine not ready.", 400)
    try:
        driveboard.job(json.loads(job))
    except ValueError as e:
        raise bottle.HTTPResponse(str(e), 422) from e
    return "{}"


@bottle.route("/optimize_fill", method="POST")
def optimize_fill():
    """Optimize fill path data according to fill_mode setting.

    This allows the frontend to preview the optimized fill path.

    Args (via POST JSON):
        data: Fill path data (list of polylines)

    Returns:
        JSON with optimized path data
    """
    try:
        request_data = json.loads(bottle.request.body.read().decode("utf-8"))
        fill_data = request_data.get("data", [])
        fill_mode = conf["fill_mode"]
        tolerance = conf["tolerance"]

        # Make a copy to avoid modifying the original
        optimized = [list(seg) for seg in fill_data]

        if fill_mode == "Forward":
            pass  # No optimization
        elif fill_mode == "Reverse":
            pathoptimizer.reverse_path(optimized)
        elif fill_mode == "Bidirectional":
            pathoptimizer.fill_optimize(optimized, tolerance)
        elif fill_mode == "NearestNeighbor":
            pathoptimizer.optimize(optimized, tolerance)

        return json.dumps({"data": optimized, "fill_mode": fill_mode})
    except Exception as e:
        traceback.print_exc()
        raise bottle.HTTPResponse(f"Error optimizing fill: {str(e)}", 400) from e


# The job view's preview, computed off the request thread. The server serves
# one request at a time, so ordering a big job inline would stall the status
# polling and the stop button behind it. Only the newest job matters: a request
# arriving mid-computation replaces the pending one, and the frontend collects
# the answer by polling with the token it was handed.
_preview_lock = threading.Lock()
_preview_worker = None
_preview_request = None  # (token, job) waiting to be computed
_preview_result = None  # (token, result dict or error string)
_preview_token = 0
# Ordering a job is the expensive part of a preview, and most of what prompts
# one leaves the job alone (widgets opening and closing, a colour reassigned
# and put back). The answer for an unchanged job and unchanged settings is
# unchanged too, so the last one is kept and handed straight back.
_preview_cache = None  # (key, result)


def _preview_key(body):
    """What a preview depends on: the job as posted and the settings that
    change what a run of it would do."""
    settings = json.dumps(conf, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha1(body).hexdigest() + hashlib.sha1(settings).hexdigest()


def _preview_run():
    """Compute queued previews until none is left, newest first."""
    global _preview_worker, _preview_request, _preview_result, _preview_cache
    while True:
        with _preview_lock:
            if _preview_request is None:
                _preview_worker = None
                return
            token, job, key, optimize = _preview_request
            _preview_request = None
        try:
            if optimize:
                # the same pass /load makes on the way to a run, so the
                # preview describes what the machine would be sent
                jobimport.optimize_job(job, conf["tolerance"])
            result = driveboard.job_preview(job)
            outcome = (token, result)
        except Exception as e:
            traceback.print_exc()
            outcome = (token, {"error": str(e)})
        with _preview_lock:
            if "error" not in outcome[1]:
                _preview_cache = (key, outcome[1])
            # a newer request may have landed while this one ran, and its
            # answer supersedes this one
            if _preview_request is None or _preview_request[0] < token:
                _preview_result = outcome


@bottle.route("/job_preview", method="POST")
def job_preview():
    """Queue the seek lines and run time of a job, for the job view.

    The job goes through the same fill_mode optimization /load applies on the
    way to a run, so the preview describes what the machine would be sent.
    Optimizing and ordering both happen on a worker thread, leaving this
    handler with only the parse; poll /job_preview/<token> for the answer.

    Args (via POST JSON):
        job: dba-style dict with passes/items/defs. Image defs only need
             kind/pos/size, pixel data is not used.
        optimize: flag whether to optimize (bool), default True, matching
             /load

    Returns:
        JSON with the token to collect the result under.
    """
    global _preview_worker, _preview_request, _preview_result, _preview_token
    try:
        body = bottle.request.body.read()
        key = _preview_key(body)
        request_data = json.loads(body.decode("utf-8"))
        job = request_data.get("job", {})
        optimize = bool(request_data.get("optimize", True))
    except Exception as e:
        traceback.print_exc()
        raise bottle.HTTPResponse(f"Error reading job: {str(e)}", 400) from e
    with _preview_lock:
        _preview_token += 1
        token = _preview_token
        cached = _preview_cache
        if cached is not None and cached[0] == key:
            # nothing that matters changed, so the answer is already known
            _preview_result = (token, cached[1])
            _preview_request = None
            return json.dumps({"token": token})
        _preview_request = (token, job, key, optimize)
        if _preview_worker is None:
            _preview_worker = threading.Thread(target=_preview_run, daemon=True)
            _preview_worker.start()
    return json.dumps({"token": token})


@bottle.route("/job_preview/<token:int>")
def job_preview_result(token):
    """Collect a queued preview. Returns {"pending": true} until it is done."""
    with _preview_lock:
        result = _preview_result
    if result is None or result[0] != token:
        return json.dumps({"pending": True})
    if "error" in result[1]:
        raise bottle.HTTPResponse(f"Error computing preview: {result[1]['error']}", 400)
    return json.dumps(result[1])


@bottle.route("/pause")
@bottle.auth_basic(checkuser)
@checkserial
def pause():
    """Pause a job gracefully."""
    driveboard.pause()
    return "{}"


@bottle.route("/unpause")
@bottle.auth_basic(checkuser)
@checkserial
def unpause():
    """Resume a paused job."""
    driveboard.unpause()
    return "{}"


@bottle.route("/stop")
@bottle.auth_basic(checkuser)
@checkserial
def stop_():
    """Halt machine immediately and purge job."""
    driveboard.stop()
    return "{}"


@bottle.route("/unstop")
@bottle.auth_basic(checkuser)
@checkserial
def unstop():
    """Recover machine from stop mode."""
    driveboard.unstop()
    return "{}"


### MCU MANAGMENT


@bottle.route("/build")
@bottle.auth_basic(checkuser)
def build():
    """Build firmware from firmware/src files (for all config files)."""
    return_code = driveboard.build()
    if return_code != 0:
        raise bottle.HTTPResponse("Build failed.", 400)
    else:
        return "{}"


@bottle.route("/flash")
@bottle.route("/flash/<firmware>")
@bottle.auth_basic(checkuser)
def flash(firmware=None):
    """Flash firmware to MCU."""
    if firmware is None:
        return_code = driveboard.flash()
    else:
        return_code = driveboard.flash(firmware=firmware)
    if return_code != 0:
        raise bottle.HTTPResponse("Flashing failed.", 400)
    else:
        return "{}"


@bottle.route("/reset")
@bottle.auth_basic(checkuser)
def reset():
    """Reset MCU"""
    try:
        driveboard.reset()
    except OSError:
        raise bottle.HTTPResponse("Reset failed.", 400) from None
    return "{}"


@bottle.route("/hello/<name>")
def hello(name):
    return bottle.template("<b>Hello {{name}}</b>!", name=name)


###############################################################################
###############################################################################


class ThreadingWSGIServer(socketserver.ThreadingMixIn, wsgiref.simple_server.WSGIServer):
    """A request per thread, so a slow one cannot hold up the rest.

    Queueing a job builds its whole command stream in the request that asked
    for it, and the machine is already burning the start of it long before
    that returns. Served one at a time, the stop button would sit unanswered
    for as long as that takes, which on a big raster is tens of seconds. The
    same goes for pause, and for the status the interlocks are reported in.
    """

    daemon_threads = True


class Server(threading.Thread):
    def __init__(self):
        threading.Thread.__init__(self)
        self.server = None
        self.lock = threading.Lock()
        self.stop_server = False

    def run(self):
        while 1:
            try:
                with self.lock:
                    if self.stop_server:
                        break
                self.server.handle_request()
            except KeyboardInterrupt:
                break
        print("\nServer shutting down...")
        driveboard.close()

    def stop(self):
        with self.lock:
            self.stop_server = True
        self.join()


S = Server()


def start(browser=False, debug=False):
    """Start a bottle web server.
    Derived from WSGIRefServer.run()
    to have control over the main loop.
    """
    global DEBUG
    DEBUG = debug

    class FixedHandler(wsgiref.simple_server.WSGIRequestHandler):
        def address_string(self):  # Prevent reverse DNS lookups please.
            return self.client_address[0]

        def log_request(*args, **kw):
            if debug:
                return wsgiref.simple_server.WSGIRequestHandler.log_request(*args, **kw)

    S.server = wsgiref.simple_server.make_server(
        conf["network_host"],
        conf["network_port"],
        bottle.default_app(),
        ThreadingWSGIServer,
        FixedHandler,
    )
    S.server.timeout = 0.01
    S.server.quiet = not debug
    if debug:
        bottle.debug(True)
    print("Library Directory: " + conf["rootdir"])
    print("Config Directory: " + conf["confdir"])
    print("Queue Directory: " + conf["stordir"])
    print("-----------------------------------------------------------------------------")
    print(f"Starting server at http://127.0.0.1:{conf['network_port']}/")
    print("-----------------------------------------------------------------------------")
    driveboard.connect_withfind()
    # open web-browser
    if browser:
        try:
            webbrowser.open_new_tab("http://127.0.0.1:" + str(conf["network_port"]))
        except webbrowser.Error:
            print("Cannot open Webbrowser, please do so manually.")
    sys.stdout.flush()  # make sure everything gets flushed
    # start server
    # print "INFO: Starting web server thread."
    S.start()
    driveboard.air_off()
    if conf["home_on_startup"]:
        try:
            # will fail if board not flashed
            driveboard.homing()
        except Exception:
            pass


def stop():
    global S
    S.stop()
    # recreate server to unbind
    # and allow restarting
    del S
    Server()


if __name__ == "__main__":
    start()
    while 1:  # wait until keyboard interrupt
        try:
            time.sleep(0.1)
        except KeyboardInterrupt:
            break
    stop()
    print("END of DriveboardApp")
