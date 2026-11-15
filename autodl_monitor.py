# -*- coding: utf-8 -*-
"""AutoDL GPU 空闲监控 / 抢卡工具
用法: python autodl_monitor.py 然后打开 http://127.0.0.1:8917
"""
import base64
import json
import os
import random
import re
import shutil
import ssl
import tempfile
import threading
import time
import urllib.request
import urllib.error
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
HTML_PATH = os.path.join(BASE_DIR, "monitor.html")
API = "https://www.autodl.com/"
PORT = 8917

DEFAULT_CONFIG = {
    "token": "",
    "interval": 3,               # 轮询间隔(秒)
    "gpu_names": [],             # 监控的卡型
    "region_sign_list": [],      # 限定区域, 空=全部
    "req_gpu_amount": 1,         # 每台至少需要的空闲卡数
    "max_price": 0,              # 最高单价(元/小时), 0=不限
    "auto_open_page": True,      # 发现空闲自动打开创建页
    "auto_create": False,        # 自动创建实例(抢)
    "auto_stop_after_create": True,
    "base_image_uuid": "",
    "private_image_uuid": "",
    "instance_name": "",
    "webhook_url": "",           # 可选: bark/企业微信等 GET webhook, {msg} 会被替换
    "notify_sound": True,
    "alert_mode": "first",       # first=卡型从无到有才报; new=每台新空闲机器都报
    "alert_cooldown": 30,        # 两次报警最小间隔(秒)
    "alarm_duration": 8,         # 响铃时长(秒), 0=一直响到手动关闭
    "verify_ttl": 10,            # checkOnline 结果缓存秒数, 降低请求量
    "grab_window_enable": False, # 限定时间段自动开抢
    "grab_start": "00:00",
    "grab_end": "23:59",
    "charge_type": "payg",       # payg 按量 / daily 包日 / weekly 包周 / monthly 包月
    "prepay_duration": 1,        # 预付费时长(包日=天数,包周=周数,包月=月数)
    "expand_data_disk_gb": 0,    # 数据盘扩容 GB
    "grab_mode": "image",        # image=新建镜像实例 / clone=克隆已有实例
    "clone_source_uuid": "",     # 克隆源实例 uuid (须保持关机)
    "clone_copy_data_disk": True,# 克隆时同时拷贝数据盘
    "clone_ft_sparse": True,     # 克隆时开启稀疏文件优化
    "watch_boot": True,          # 监控已关机实例的宿主机空闲(可否开机)
    "watch_boot_uuids": None,    # None=监控全部关机实例; 列表=只监控这些; []=都不监控
    "auto_power_on": False,      # 实例变为可开机时自动开机(立即计费,谨慎)
    "created_count": 0,
}

SSL_CTX = ssl.create_default_context()


def _dpapi(data, unprotect):
    """Windows DPAPI: 仅当前机器+当前用户可解. 非 Windows 返回 None 走明文."""
    if os.name != "nt":
        return None
    import ctypes
    import ctypes.wintypes as wt

    class _BLOB(ctypes.Structure):
        _fields_ = [("cbData", wt.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    fun = (ctypes.windll.crypt32.CryptUnprotectData if unprotect
           else ctypes.windll.crypt32.CryptProtectData)
    fun.argtypes = [ctypes.POINTER(_BLOB), wt.LPCWSTR, ctypes.POINTER(_BLOB),
                    wt.LPVOID, wt.LPVOID, wt.DWORD, ctypes.POINTER(_BLOB)]
    fun.restype = wt.BOOL
    ibuf = ctypes.create_string_buffer(data, len(data))
    ib = _BLOB(len(data), ibuf)
    ebuf = ctypes.create_string_buffer(b"autodl-sniper-v1", 16)
    ent = _BLOB(16, ebuf)
    ob = _BLOB()
    if not fun(ctypes.byref(ib), None, ctypes.byref(ent), None, None, 0,
               ctypes.byref(ob)):
        raise RuntimeError("DPAPI call failed")
    try:
        return ctypes.string_at(ob.pbData, ob.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(ob.pbData)


TOKEN_ENC_PREFIX = "dpapi:"


def token_encrypt(tok):
    """明文 -> 存储串; 加密不可用时原样返回(非 Windows)."""
    enc = _dpapi(tok.encode("utf-8"), unprotect=False)
    if enc is None:
        return tok
    return TOKEN_ENC_PREFIX + base64.b64encode(enc).decode()


def token_decrypt(stored):
    """存储串 -> 明文; 前缀缺失/解密失败返回 ''."""
    if not stored or not stored.startswith(TOKEN_ENC_PREFIX):
        return ""
    try:
        raw = _dpapi(base64.b64decode(stored[len(TOKEN_ENC_PREFIX):]),
                     unprotect=True)
        return raw.decode("utf-8") if raw else ""
    except Exception:
        return ""


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg.update(json.load(f))
    except Exception:
        pass
    # 新版: 明文 token 不落盘, 解密 token_enc; 解密失败则留空由 rescue 接回
    if not cfg.get("token") and cfg.get("token_enc"):
        cfg["token"] = token_decrypt(cfg["token_enc"])
    return cfg


def save_config():
    cfg = dict(STATE["config"])
    tok = cfg.get("token") or ""
    if tok and os.name == "nt":
        cfg["token"] = ""
        cfg["token_enc"] = token_encrypt(tok)
    else:
        cfg["token_enc"] = cfg.get("token_enc") or ""
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def _is_sub_user_token(tok):
    """JWT payload 含 sub_name 则为子账号."""
    try:
        part = tok.split(".")[1]
        part += "=" * (-len(part) % 4)
        payload = json.loads(base64.urlsafe_b64decode(part))
        return bool(payload.get("sub_name"))
    except Exception:
        return False


def api_request(path, method="GET", body=None, auth=True, timeout=15, token=None):
    tok = token if token is not None else STATE["config"].get("token", "")
    # 官方前端: 子账号请求统一改写 api/v1/ -> api/v1/sub_user/ (少量白名单除外)
    if auth and tok and _is_sub_user_token(tok) and path.startswith("api/v1/"):
        if not any(path.startswith(p) for p in
                   ("api/v1/file_storage/", "api/v1/machine_pro/", "api/v1/wx/", "api/v1/net_disk")):
            path = "api/v1/sub_user/" + path[len("api/v1/"):]
    url = API + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", "Mozilla/5.0")
    req.add_header("AppVersion", "0.0.0")
    if auth and tok:
        req.add_header("Authorization", tok)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode()[:200]
        except Exception:
            pass
        raise RuntimeError("HTTP %s %s" % (e.code, body))


STATE = {
    "config": load_config(),
    "running": False,
    "last_poll": 0,
    "poll_ms": 0,
    "summary": [],          # [{gpu_name, idle_machines, idle_gpus, min_price}]
    "candidates": [],       # 空闲机器列表
    "alert_seq": 0,
    "prev_ids": set(),
    "prev_avail": set(),         # 上一轮有空闲候选的卡型
    "last_alert": 0.0,
    "last_grab": 0.0,
    "verify_cache": {},          # machine_id -> (ts, 在线可用)
    "consec_errors": 0,
    "logs": [],
    "create_result": None,
    "token_error": False,
    "token_error_msg": "",
    "stop": threading.Event(),
    "clone_src": {"ts": 0.0, "uuid": "", "info": None},   # 克隆源信息缓存
    "instances": [],             # 我的实例(含宿主机空闲卡数)
    "prev_bootable": set(),      # 上一轮可开机的实例 uuid
    "alert_msg": "",             # 最近一次报警内容
    "alert_kinds": {},           # 报警构成 {market: bool, boot: bool}
}
LOCK = threading.Lock()


def log(msg):
    line = time.strftime("%H:%M:%S") + "  " + msg
    print(line, flush=True)
    with LOCK:
        STATE["logs"].append(line)
        if len(STATE["logs"]) > 300:
            STATE["logs"] = STATE["logs"][-300:]


def search_machines(gpu_names, req_num, regions, page_index=1, page_size=500):
    body = {
        "charge_type": "payg",
        "region_sign": "",
        "gpu_type_name": gpu_names,
        "machine_tag_name": [],
        "gpu_idle_num": req_num,
        "mount_net_disk": False,
        "instance_disk_size_order": "",
        "date_range": "",
        "date_from": "",
        "date_to": "",
        "page_index": page_index,
        "page_size": page_size,
        "pay_price_order": "",
        "gpu_idle_type": "",
        "default_order": True,
        "region_sign_list": regions,
    }
    # 官网市场页实际用 user/machine/list (findAll); 子账号下自动变为 sub_user/user/machine/list
    return api_request("api/v1/user/machine/list", "POST", body)


def fetch_all_idle(gpu_names, req_num, regions):
    machines, total = [], 0
    for page in (1, 2, 3):
        resp = search_machines(gpu_names, req_num, regions, page_index=page)
        if resp.get("code") != "Success":
            raise RuntimeError(resp.get("msg") or resp.get("code") or "search failed")
        data = resp.get("data") or {}
        machines.extend(data.get("list") or [])
        total = data.get("result_total") or 0
        if len(machines) >= total:
            break
    return machines, total


def pick_candidates(machines, req_num, max_price):
    out = []
    for m in machines:
        if m.get("on_line") in (3, 4, 5, 6):
            continue
        if (m.get("gpu_idle_num") or 0) < req_num:
            continue
        if m.get("max_instance_num", 0) <= m.get("binding_instance_num", 0):
            continue
        price = (m.get("payg_price") or 0) / 1000.0
        if max_price and price > max_price:
            continue
        out.append({
            "machine_id": m.get("machine_id"),
            "alias": m.get("machine_alias") or m.get("machine_name"),
            "gpu_name": m.get("gpu_name"),
            "region_name": m.get("region_name"),
            "region_sign": m.get("region_sign"),
            "idle": m.get("gpu_idle_num"),
            "total_gpu": m.get("gpu_number"),
            "price": price,
            "cuda": m.get("highest_cuda_version"),
            "cpu_arch": m.get("cpu_arch"),
            "chip_corp": m.get("chip_corp"),
            "data_disk": m.get("max_instance_disk_size"),
        })
    out.sort(key=lambda x: x["price"])
    return out


def check_online(machine_id, ttl):
    ent = STATE["verify_cache"].get(machine_id)
    if ent and time.time() - ent[0] < ttl:
        return ent[1]
    try:
        r = api_request("api/v1/machine/check_machine_online", "POST",
                        {"machine_id": machine_id})
        ok = r.get("data") == 2
    except Exception:
        ok = True   # 校验失败不丢弃, 由 create 接口兜底
    STATE["verify_cache"][machine_id] = (time.time(), ok)
    return ok


def in_grab_window(cfg):
    if not cfg.get("grab_window_enable"):
        return True
    now = time.strftime("%H:%M")
    s = cfg.get("grab_start") or "00:00"
    e = cfg.get("grab_end") or "23:59"
    return s <= now <= e if s <= e else (now >= s or now <= e)


def create_instance(machine_id, num, cfg):
    ct = cfg.get("charge_type") or "payg"
    disk = int(cfg.get("expand_data_disk_gb") or 0) * 1024 ** 3
    dur = 1 if ct == "payg" else int(cfg.get("prepay_duration") or 1)
    body = {
        "instance_info": {
            "machine_id": machine_id,
            "charge_type": ct,
            "req_gpu_amount": num,
            "base_image_uuid": cfg.get("base_image_uuid") or "",
            "private_image_uuid": cfg.get("private_image_uuid") or "",
            "reproduction_uuid": "",
            "reproduction_id": 0,
            "cg_application_uuid": "",
            "cg_application_info": {"image_name": "", "current_version_id": 0,
                                  "current_version": "", "image_id": 0},
            "instance_name": cfg.get("instance_name") or "",
            "expand_data_disk": disk,
        },
        "price_info": {
            "coupon_id_list": [],
            "machine_id": machine_id,
            "charge_type": ct,
            "duration": dur,
            "num": num,
            "expand_data_disk": disk,
        },
    }
    ep = "api/v1/order/instance/create/payg" if ct == "payg" \
        else "api/v1/order/instance/create/prepay"
    return api_request(ep, "POST", body)


def list_instances():
    r = api_request("api/v1/instance", "POST",
                    {"page_index": 1, "page_size": 100, "status": [], "charge_type": []})
    if r.get("code") != "Success":
        raise RuntimeError(r.get("msg") or r.get("code") or "list failed")
    return [{
        "uuid": i.get("uuid"),
        "name": i.get("name") or "",
        "status": i.get("status") or "",
        "gpu": i.get("snapshot_gpu_alias_name") or "",
        "region_name": i.get("region_name") or "",
        "machine_id": i.get("machine_id") or "",
        "machine_alias": i.get("machine_alias") or "",
        "charge_type": i.get("charge_type") or "",
        "req_gpu_amount": i.get("req_gpu_amount") or 1,
        "gpu_idle_num": i.get("gpu_idle_num"),
        "gpu_all_num": i.get("gpu_all_num"),
        "start_mode": i.get("start_mode") or "",
        "bootable": i.get("status") == "shutdown"
                    and (i.get("gpu_idle_num") or 0) >= (i.get("req_gpu_amount") or 1),
        "data_disk_used": (i.get("usage_info") or {}).get("data_disk_used_size") or 0,
    } for i in (r.get("data") or {}).get("list") or []]


def power_on(uuid):
    return api_request("api/v1/instance/power_on", "POST", {"instance_uuid": uuid})


def _cuda_num(v):
    try:
        return float(str(v).strip().lower().replace("cuda", "").strip())
    except Exception:
        return -1.0


def clone_source_info(uuid, ttl=30):
    """克隆源实例的快照信息: 镜像/架构/CUDA/数据盘用量. 结果缓存 ttl 秒."""
    ent = STATE["clone_src"]
    if ent["uuid"] == uuid and ent["info"] and time.time() - ent["ts"] < ttl:
        return ent["info"]
    q = urllib.parse.quote(uuid)
    dd = (api_request("api/v1/instance/detail?instance_uuid=" + q).get("data") or {})
    sd = (api_request("api/v1/instance/snapshot?instance_uuid=" + q).get("data") or {})
    img = sd.get("image") or {}
    # 关机时 detail 的 usage_info 归零, 数据盘用量以实例列表快照为准
    used = (dd.get("usage_info") or {}).get("data_disk_used_size") or 0
    try:
        for li in list_instances():
            if li["uuid"] == uuid:
                used = max(used, li.get("data_disk_used") or 0)
    except Exception:
        pass
    info = {
        "uuid": dd.get("instance_uuid") or uuid,
        "name": dd.get("instance_name") or "",
        "status": dd.get("status") or "",
        "base_image_uuid": img.get("base_image_uuid") or "",
        "cuda": str(img.get("cuda_version") or ""),
        "cpu_arch": img.get("cpu_arch") or sd.get("cpu_arch") or "",
        "chip_corp": img.get("chip_corp") or sd.get("chip_corp") or "",
        "data_disk_used": used,
        "expand_data_disk_size": dd.get("data_disk_expand_size") or 0,
        "framework": ("%s %s" % (img.get("framework_name") or "",
                                 img.get("framework_version") or "")).strip(),
        "image_type": img.get("image_type") or "",
    }
    STATE["clone_src"] = {"ts": time.time(), "uuid": uuid, "info": info}
    return info


def clone_compatible(src, c):
    """目标机器兼容: 同 CPU 架构、同芯片厂商, 且机器最高 CUDA >= 源镜像 CUDA."""
    if src["cpu_arch"] and c.get("cpu_arch") and src["cpu_arch"] != c["cpu_arch"]:
        return False
    if src["chip_corp"] and c.get("chip_corp") and src["chip_corp"] != c["chip_corp"]:
        return False
    mc = _cuda_num(c.get("cuda"))
    return mc < 0 or mc >= _cuda_num(src["cuda"])


def clone_instance(machine_id, num, cfg, src):
    ct = cfg.get("charge_type") or "payg"
    disk = max(int(cfg.get("expand_data_disk_gb") or 0) * 1024 ** 3,
               int(src.get("expand_data_disk_size") or 0))
    dur = 1 if ct == "payg" else int(cfg.get("prepay_duration") or 1)
    body = {
        "instance_uuid": src["uuid"],
        "instance_info": {
            "machine_id": machine_id,
            "charge_type": ct,
            "req_gpu_amount": num,
            "base_image_uuid": src["base_image_uuid"],
            "private_image_uuid": "",
            "reproduction_uuid": "",
            "reproduction_id": 0,
            "cg_application_uuid": "",
            "cg_application_info": {},
            "instance_name": cfg.get("instance_name") or "",
            "expand_data_disk": disk,
            "copy_data_disk_after_clone": bool(cfg.get("clone_copy_data_disk", True)),
            "keep_src_user_service_address_after_clone": False,
            "ft_sparse": bool(cfg.get("clone_ft_sparse", True)),
        },
        "price_info": {
            "coupon_id_list": [],
            "machine_id": machine_id,
            "charge_type": ct,
            "duration": dur,
            "num": num,
            "expand_data_disk": disk,
        },
    }
    ep = "api/v1/order/instance/clone/payg" if ct == "payg" \
        else "api/v1/order/instance/clone/prepay"
    return api_request(ep, "POST", body)


def fire_webhook(msg):
    url = STATE["config"].get("webhook_url") or ""
    if not url:
        return
    try:
        urllib.request.urlopen(url.replace("{msg}", urllib.parse.quote(msg)), timeout=8, context=SSL_CTX)
    except Exception as e:
        log("webhook 发送失败: %s" % e)


def _snappy_decode(data):
    """解压 raw snappy 块 (leveldb 用的非 framed 格式)."""
    pos = 0
    res = 0
    shift = 0
    while pos < len(data):
        b = data[pos]
        pos += 1
        res |= (b & 0x7f) << shift
        if not b & 0x80:
            break
        shift += 7
    out = bytearray()
    n = len(data)
    while pos < n:
        tag = data[pos] & 3
        if tag == 0:
            ln = data[pos] >> 2
            pos += 1
            if ln < 60:
                ln += 1
            else:
                nb = ln - 59
                ln = int.from_bytes(data[pos:pos + nb], "little") + 1
                pos += nb
            out += data[pos:pos + ln]
            pos += ln
        else:
            if tag == 1:
                ln = 4 + ((data[pos] >> 2) & 7)
                off = ((data[pos] & 0xe0) << 3) | data[pos + 1]
                pos += 2
            elif tag == 2:
                ln = 1 + (data[pos] >> 2)
                off = int.from_bytes(data[pos + 1:pos + 3], "little")
                pos += 3
            else:
                ln = 1 + (data[pos] >> 2)
                off = int.from_bytes(data[pos + 1:pos + 5], "little")
                pos += 5
            end = len(out)
            for i in range(ln):
                out.append(out[end - off + i])
    return bytes(out)


def _varint(buf, pos):
    res = 0
    shift = 0
    while pos < len(buf):
        b = buf[pos]
        pos += 1
        res |= (b & 0x7f) << shift
        if not b & 0x80:
            return res, pos
        shift += 7
    return res, pos


def _read_file_maybe_locked(fp):
    try:
        with open(fp, "rb") as f:
            return f.read()
    except Exception:
        try:
            tmp = os.path.join(tempfile.gettempdir(),
                               "adl_ls_%d_%s" % (os.getpid(), os.path.basename(fp)))
            shutil.copyfile(fp, tmp)
            with open(tmp, "rb") as f:
                data = f.read()
            os.remove(tmp)
            return data
        except Exception:
            return b""


def _sstable_blocks(raw):
    """解析 .ldb SSTable: 返回 原文 + 解压后所有数据块 的拼接."""
    if len(raw) < 48 or raw[-8:] != b"\x57\xfb\x80\x8b\x24\x75\x47\xdb":
        return b""
    try:
        _mo, p = _varint(raw, len(raw) - 48)
        _ms, p = _varint(raw, p)
        ioff, p = _varint(raw, p)
        isize, p = _varint(raw, p)
    except Exception:
        return b""

    def get_block(off, size):
        if off + size + 5 > len(raw):
            return b""
        blk = raw[off:off + size]
        ctype = raw[off + size]
        if ctype == 1:
            try:
                return _snappy_decode(blk)
            except Exception:
                return b""
        return blk

    idx = get_block(ioff, isize)
    out = []
    pos = 0
    last_key = b""
    while pos < len(idx):
        try:
            shared, pos = _varint(idx, pos)
            nonshared, pos = _varint(idx, pos)
            vlen, pos = _varint(idx, pos)
        except Exception:
            break
        last_key = last_key[:shared] + idx[pos:pos + nonshared]
        pos += nonshared
        val = idx[pos:pos + vlen]
        pos += vlen
        try:
            o, q = _varint(val, 0)
            s, q = _varint(val, q)
        except Exception:
            continue
        out.append(get_block(o, s))
    return b"".join(out)


def _scan_leveldb_tokens():
    """从本机 Chromium 系浏览器的 Local Storage leveldb 中收集 autodl token 候选.
    只能读到未压缩的 .log/.ldb 明文记录; 返回去重后的候选列表(可能含过期token)."""
    lad = os.environ.get("LOCALAPPDATA", "")
    rad = os.environ.get("APPDATA", "")
    exp = os.path.expanduser
    homes = [
        # Windows
        os.path.join(lad, "Google", "Chrome", "User Data"),
        os.path.join(lad, "Microsoft", "Edge", "User Data"),
        os.path.join(lad, "Quark", "User Data"),
        os.path.join(lad, "360Chrome", "Chrome", "User Data"),
        os.path.join(lad, "360se6", "User Data"),
        os.path.join(lad, "QQBrowser", "User Data"),
        os.path.join(lad, "BraveSoftware", "Brave-Browser", "User Data"),
        os.path.join(lad, "SogouExplorer"),
        os.path.join(rad, "SogouExplorer"),
        # macOS
        exp("~/Library/Application Support/Google/Chrome"),
        exp("~/Library/Application Support/Microsoft Edge"),
        exp("~/Library/Application Support/BraveSoftware/Brave-Browser"),
        exp("~/Library/Application Support/Chromium"),
        # Linux
        exp("~/.config/google-chrome"),
        exp("~/.config/microsoft-edge"),
        exp("~/.config/chromium"),
        exp("~/.config/BraveSoftware/Brave-Browser"),
    ]
    pat_ascii = re.compile(rb"autodl\.com")
    pat_utf16 = "autodl.com".encode("utf-16-le")
    pat_tok_a = re.compile(rb"token")
    pat_tok_u = "token".encode("utf-16-le")
    pat_jwt = re.compile(rb"eyJ[\w-]{10,}\.[\w-]{10,}\.[\w-]{10,}")  # JWT 形状优先
    pat_val = re.compile(rb"[\x20-\x7e]{40,700}")          # ASCII 候选值
    pat_val_u = re.compile(rb"(?:[\x20-\x7e]\x00){40,700}")  # UTF-16LE 候选值
    found = []
    for home in homes:
        if not os.path.isdir(home):
            continue
        for dirpath, _dirs, files in os.walk(home):
            if "Local Storage" not in dirpath:
                continue
            for fn in files:
                if not (fn.endswith(".log") or fn.endswith(".ldb")):
                    continue
                fp = os.path.join(dirpath, fn)
                raw = _read_file_maybe_locked(fp)
                if not raw:
                    continue
                data = raw + (_sstable_blocks(raw) if fn.endswith(".ldb") else b"")
                if not pat_ascii.search(data) and pat_utf16 not in data:
                    continue
                spots = [m.end() for m in pat_ascii.finditer(data)]
                u = data.find(pat_utf16)
                if u >= 0:
                    spots.append(u + len(pat_utf16))
                for sp in spots:
                    seg = data[sp:sp + 4000]
                    # 注意: leveldb 前缀压缩下 'token' 键可能只存 'oken',
                    # 不能要求段内出现完整 'token' 字样
                    cands = list(pat_jwt.findall(seg))
                    # token 键后紧跟的值: ASCII 和 UTF-16 两种定位
                    for tm in pat_tok_a.finditer(seg):
                        vs = seg[tm.end():tm.end() + 1500]
                        cands += pat_jwt.findall(vs)
                        cands += pat_val.findall(vs)
                    for m2 in re.finditer(pat_tok_u, seg):
                        vs = seg[m2.end():m2.end() + 3000]
                        cands += [b.decode("utf-16-le", "ignore").encode()
                                  for b in pat_val_u.findall(vs)]
                    for c in cands:
                        t = c.decode("utf-8", "ignore").strip("\x00 ")
                        if len(t) > 60 and "token" not in t[:20].lower() \
                                and " " not in t and t[0] not in "{[\"'" \
                                and t not in found:
                            found.append(t)
    return found


def _token_alive(tok):
    # wallet/gpu_type 都不能用来验证: 前者子账号没权限, 后者是匿名公开接口.
    # 规律: 死token一律 AuthorizeFailed; 活token过了鉴权层, 参数/权限问题会报 BadRequest
    try:
        r = api_request("api/v1/machine/search", "POST", {}, token=tok)
        return r.get("code") != "AuthorizeFailed"
    except urllib.error.HTTPError:
        return False
    except Exception:
        return False


def _scan_leveldb_token():
    """返回第一个验证有效的 token, 没有活的返回 None."""
    for tok in _scan_leveldb_tokens():
        if _token_alive(tok):
            return tok
    return None


def token_rescue_loop():
    """token 缺失/失效时, 周期扫描本机浏览器 Local Storage 自动接回新登录的 token."""
    while True:
        try:
            cfg = STATE["config"]
            need = (not cfg.get("token")) or STATE.get("token_error")
            if need:
                for tok in _scan_leveldb_tokens():
                    if tok != cfg.get("token") and _token_alive(tok):
                        cfg["token"] = tok
                        save_config()
                        STATE["token_error"] = False
                        log("检测到新的浏览器登录态, token 已自动更新")
                        break
            time.sleep(15)
        except Exception:
            time.sleep(15)


def monitor_loop(stop_ev):
    log("监控已启动")
    idle_hinted = False
    while not stop_ev.is_set():
        cfg = STATE["config"]
        gpu_names = cfg.get("gpu_names") or []
        watch = cfg.get("watch_boot", True)
        if not cfg.get("token") or (not gpu_names and not watch):
            if not idle_hinted:
                idle_hinted = True
                log("等待配置: %s" % ("未配置 token" if not cfg.get("token")
                                      else "未选择监控卡型/实例开机监控"))
            time.sleep(1)
            continue
        idle_hinted = False
        t0 = time.time()
        try:
            cands, cur_ids, cur_avail, summary = [], set(), set(), {}
            trigger = []
            now = time.time()
            cooldown = float(cfg.get("alert_cooldown") or 0)
            alert_parts = []
            if gpu_names:
                machines, _ = fetch_all_idle(gpu_names, cfg.get("req_gpu_amount") or 1,
                                           cfg.get("region_sign_list") or [])
                cands = pick_candidates(machines, cfg.get("req_gpu_amount") or 1,
                                        cfg.get("max_price") or 0)
                ghosts = 0
                verified = []
                ttl = float(cfg.get("verify_ttl") or 10)
                for c in cands[:15]:
                    if check_online(c["machine_id"], ttl):
                        verified.append(c)
                    else:
                        ghosts += 1
                if ghosts:
                    log("过滤 %d 台幽灵空闲(search有缓存已下线)" % ghosts)
                cands = verified
                for c in cands:
                    s = summary.setdefault(c["gpu_name"], {"gpu_name": c["gpu_name"],
                                           "idle_machines": 0, "idle_gpus": 0, "min_price": None})
                    s["idle_machines"] += 1
                    s["idle_gpus"] += c["idle"]
                    if s["min_price"] is None or c["price"] < s["min_price"]:
                        s["min_price"] = c["price"]
                cur_ids = {c["machine_id"] + "|" + c["gpu_name"] for c in cands}
                cur_avail = {c["gpu_name"] for c in cands}
                if cfg.get("alert_mode") == "new":
                    trigger = sorted({i.split("|")[1] for i in (cur_ids - STATE["prev_ids"])})
                else:
                    trigger = sorted(cur_avail - STATE["prev_avail"])
                if trigger:
                    alert_parts.append("发现空闲: " + ",".join(trigger)
                                       + " (%d台)" % len(cur_ids))
            boot_names, new_boot = [], set()
            if watch:
                try:
                    insts = list_instances()
                except Exception as e:
                    insts = None
                    log("实例状态查询失败: %s" % e)
                if insts is not None:
                    sel = cfg.get("watch_boot_uuids")
                    watched = insts if sel is None \
                        else [i for i in insts if i["uuid"] in set(sel)]
                    bootable = {i["uuid"] for i in watched if i.get("bootable")}
                    new_boot = bootable - STATE["prev_bootable"]
                    boot_names = [i.get("machine_alias") or i["uuid"][:12]
                                  for i in insts if i["uuid"] in new_boot]
                    with LOCK:
                        STATE["instances"] = insts
                        STATE["prev_bootable"] = bootable
                    if boot_names:
                        alert_parts.append("实例可开机: " + ",".join(boot_names))
            should_alert = bool(alert_parts) and (now - STATE["last_alert"] >= cooldown)
            grab_cd = max(10.0, float(cfg.get("interval") or 3))
            should_grab = (cfg.get("auto_create") and cands
                           and in_grab_window(cfg)
                           and now - STATE["last_grab"] >= grab_cd)
            with LOCK:
                STATE["summary"] = sorted(summary.values(), key=lambda x: x["gpu_name"])
                STATE["candidates"] = cands
                STATE["last_poll"] = now
                STATE["poll_ms"] = int((now - t0) * 1000)
                STATE["token_error"] = False
                STATE["token_error_msg"] = ""
                STATE["prev_ids"] = cur_ids
                STATE["prev_avail"] = cur_avail
                if should_alert:
                    STATE["alert_seq"] += 1
                    STATE["last_alert"] = now
                    STATE["alert_msg"] = "; ".join(alert_parts)
                    STATE["alert_kinds"] = {"market": bool(trigger), "boot": bool(boot_names)}
                else:   # 报警条件消失后清空横幅
                    k = STATE["alert_kinds"]
                    if k.get("market") and not cur_avail:
                        k["market"] = False
                    if k.get("boot") and not STATE["prev_bootable"]:
                        k["boot"] = False
                    if not any(k.values()):
                        STATE["alert_msg"] = ""
                if should_grab:
                    STATE["last_grab"] = now
            if should_alert:
                msg = STATE["alert_msg"]
                log(msg)
                fire_webhook(msg)
                if cfg.get("auto_open_page") and not cfg.get("auto_create") and cands:
                    c = cands[0]
                    webbrowser.open("https://www.autodl.com/create/%s/%s/%d/payg"
                                    % (c["region_sign"], c["machine_id"],
                                       cfg.get("req_gpu_amount") or 1))
            if new_boot and cfg.get("auto_power_on"):
                for i in STATE["instances"]:
                    if i["uuid"] not in new_boot:
                        continue
                    try:
                        r = power_on(i["uuid"])
                        if r.get("code") == "Success":
                            log("*** 自动开机成功: %s (%s) ***"
                                % (i.get("machine_alias"), i["uuid"]))
                            fire_webhook("AutoDL实例开机 %s" % i["uuid"])
                            webbrowser.open("https://www.autodl.com/console/instance/list")
                        else:
                            log("自动开机失败(%s): %s"
                                % (i.get("machine_alias"), r.get("msg") or r.get("code")))
                    except Exception as e:
                        log("自动开机异常(%s): %s" % (i.get("machine_alias"), e))
            if should_grab:
                try_grab(cands, cfg)
            STATE["consec_errors"] = 0
        except Exception as e:
            es = str(e)
            STATE["consec_errors"] += 1
            with LOCK:
                if "AuthorizeFailed" in es or "401" in es or "登录" in es:
                    STATE["token_error"] = True
                    STATE["token_error_msg"] = "token 已失效或被踢下线"
                elif "权限" in es or "无当前资源" in es:
                    STATE["token_error"] = True
                    STATE["token_error_msg"] = ("当前账号无算力市场访问权限"
                        "(可能登录的是子账号), 请换有权限的账号登录")
            iv = max(2.0, float(cfg.get("interval") or 3))
            backoff = min(120.0, iv * (2 ** min(STATE["consec_errors"], 5)))
            if "429" in es:
                backoff = max(60.0, backoff)
                log("触发限流(429), 退避 %.0fs" % backoff)
            else:
                log("查询失败: %s, %.0fs 后重试" % (es, backoff))
            stop_ev.wait(backoff)
            continue
        iv = max(2.0, float(cfg.get("interval") or 3))
        stop_ev.wait(iv * random.uniform(0.8, 1.2))
    # 只有仍是当前循环时才清 running, 避免覆盖新一轮 start
    if STATE["stop"] is stop_ev:
        STATE["running"] = False
    log("监控已停止")


def _order_done(resp, c, cfg, kind):
    """处理下单响应; 成功返回 True."""
    code = resp.get("code")
    if code == "Success":
        STATE["config"]["created_count"] = STATE["config"].get("created_count", 0) + 1
        save_config()
        with LOCK:
            STATE["create_result"] = {"ok": True, "machine": c, "resp": resp.get("data"),
                                      "time": time.strftime("%H:%M:%S"), "ts": time.time(),
                                      "kind": kind}
        log("*** %s成功! %s %s %s ¥%.2f/h ***" % (kind, c["gpu_name"], c["alias"], c["region_name"], c["price"]))
        fire_webhook("AutoDL%s成功 %s %s" % (kind, c["gpu_name"], c["region_name"]))
        webbrowser.open("https://www.autodl.com/console/instance/list")
        if cfg.get("auto_stop_after_create"):
            cfg["auto_create"] = False
            save_config()
        return True
    if code in ("TORecharge", "InsufficientBalance"):
        log("%s失败(%s): 余额不足, 请先充值" % (kind, c["alias"]))
    elif code == "TORealName":
        log("%s失败(%s): 需要实名认证" % (kind, c["alias"]))
    else:
        log("%s失败(%s): %s" % (kind, c["alias"], resp.get("msg") or code))
    return False


def try_grab(cands, cfg):
    if cfg.get("grab_mode") == "clone":
        try_clone_grab(cands, cfg)
        return
    if not cfg.get("base_image_uuid") and not cfg.get("private_image_uuid"):
        log("自动创建跳过: 未选择镜像(基础/私有任选其一)")
        with LOCK:
            STATE["create_result"] = {"ok": False, "time": time.strftime("%H:%M:%S"),
                                  "ts": time.time(), "msg": "未选择镜像"}
        return
    for c in cands[:3]:
        try:
            resp = create_instance(c["machine_id"], cfg.get("req_gpu_amount") or 1, cfg)
        except Exception as e:
            log("创建请求异常(%s): %s" % (c["alias"], e))
            continue
        if _order_done(resp, c, cfg, "抢卡"):
            return
    with LOCK:
        STATE["create_result"] = {"ok": False, "time": time.strftime("%H:%M:%S"),
                                  "ts": time.time()}


def try_clone_grab(cands, cfg):
    uuid = (cfg.get("clone_source_uuid") or "").strip()
    if not uuid:
        log("自动克隆跳过: 未选择源实例")
        with LOCK:
            STATE["create_result"] = {"ok": False, "time": time.strftime("%H:%M:%S"),
                                  "ts": time.time(), "msg": "未选择克隆源实例"}
        return
    try:
        src = clone_source_info(uuid)
    except Exception as e:
        log("克隆源信息获取失败: %s" % e)
        return
    if src["status"] != "shutdown":
        log("克隆源实例未关机(%s), 跳过本轮" % src["status"])
        return
    if not src["base_image_uuid"]:
        log("克隆源镜像信息缺失, 跳过本轮")
        return
    tried = 0
    for c in cands:
        if tried >= 3:
            break
        if not clone_compatible(src, c):
            continue
        if cfg.get("clone_copy_data_disk", True) and c.get("data_disk") \
                and c["data_disk"] < src["data_disk_used"]:
            log("跳过 %s: 目标数据盘 %.0fG 小于源已用 %.0fG"
                % (c["alias"], c["data_disk"] / 2 ** 30, src["data_disk_used"] / 2 ** 30))
            continue
        tried += 1
        try:
            resp = clone_instance(c["machine_id"], cfg.get("req_gpu_amount") or 1, cfg, src)
        except Exception as e:
            log("克隆请求异常(%s): %s" % (c["alias"], e))
            continue
        if _order_done(resp, c, cfg, "克隆"):
            return
    if tried:
        with LOCK:
            STATE["create_result"] = {"ok": False, "time": time.strftime("%H:%M:%S"),
                                      "ts": time.time()}


ALLOWED_ORIGINS = {
    "https://www.autodl.com",
    "http://127.0.0.1:%d" % PORT,
    "http://localhost:%d" % PORT,
}


class Handler(BaseHTTPRequestHandler):
    def _cors_origin(self):
        """只对白名单 Origin 回 ACAO; 阻止任意网站跨域读取/调用本地面板."""
        o = self.headers.get("Origin")
        return o if o in ALLOWED_ORIGINS else None

    def _origin_ok(self):
        """写操作防护: 带 Origin 的请求必须是白名单(同源/无 Origin 不受影响)."""
        o = self.headers.get("Origin")
        return o is None or o in ALLOWED_ORIGINS

    def _send(self, code, body, ctype="application/json"):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        o = self._cors_origin()
        if o:
            self.send_header("Access-Control-Allow-Origin", o)
            self.send_header("Vary", "Origin")
        self.end_headers()
        self.wfile.write(data)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False))

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def log_message(self, *a):
        pass

    def do_OPTIONS(self):
        self.send_response(204)
        o = self._cors_origin()
        if o:
            self.send_header("Access-Control-Allow-Origin", o)
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type,Authorization")
        self.end_headers()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path == "/" or path == "/index.html":
            tok = (urllib.parse.parse_qs(parsed.query).get("token") or [""])[0].strip()
            if tok:
                STATE["config"]["token"] = tok
                save_config()
                log("已从浏览器导入 token")
                self.send_response(302)
                self.send_header("Location", "/")
                self.end_headers()
                return
            with open(HTML_PATH, "rb") as f:
                self._send(200, f.read(), "text/html")
        elif path == "/api/state":
            with LOCK:
                cfg = dict(STATE["config"])
                cfg["token"] = "***" if cfg.get("token") else ""
                cfg.pop("token_enc", None)
                self._json({
                    "running": STATE["running"],
                    "last_poll": STATE["last_poll"],
                    "poll_ms": STATE["poll_ms"],
                    "summary": STATE["summary"],
                    "candidates": STATE["candidates"],
                    "instances": STATE["instances"],
                    "alert_seq": STATE["alert_seq"],
                    "alert_msg": STATE["alert_msg"],
                    "logs": STATE["logs"][-80:],
                    "create_result": STATE["create_result"],
                    "token_error": STATE["token_error"],
                    "token_error_msg": STATE["token_error_msg"],
                    "config": cfg,
                    "has_token": bool(STATE["config"].get("token")),
                    "time": time.time(),
                })
        elif path == "/api/gpu_types":
            try:
                r = api_request("api/v1/machine/gpu_type", auth=False)
                self._json(r)
            except Exception as e:
                self._json({"code": "Error", "msg": str(e)})
        elif path == "/api/regions":
            try:
                self._json(api_request("api/v1/region/list", auth=False))
            except Exception as e:
                self._json({"code": "Error", "msg": str(e)})
        elif path == "/api/images":
            try:
                base = api_request("api/v1/image/get", "POST",
                                   {"image_type": "base", "page_index": 1, "page_size": 200})
                priv = []
                try:
                    pr = api_request("api/v1/image/private/get", "POST", {})
                    if pr.get("code") == "Success" and pr.get("data"):
                        priv = pr["data"]
                except Exception:
                    pass
                self._json({"code": "Success",
                            "data": {"base": base.get("data") or [], "private": priv}})
            except Exception as e:
                self._json({"code": "Error", "msg": str(e)})
        elif path == "/api/balance":
            try:
                self._json(api_request("api/v1/wallet"))
            except Exception as e:
                self._json({"code": "Error", "msg": str(e)})
        elif path == "/api/instances":
            try:
                self._json({"code": "Success", "data": list_instances()})
            except Exception as e:
                self._json({"code": "Error", "msg": str(e)})
        elif path == "/api/clone_source":
            u = (urllib.parse.parse_qs(parsed.query).get("uuid") or [""])[0].strip()
            try:
                if not u:
                    raise RuntimeError("missing uuid")
                self._json({"code": "Success", "data": clone_source_info(u, ttl=5)})
            except Exception as e:
                self._json({"code": "Error", "msg": str(e)})
        elif path == "/api/auto_token":
            cands = _scan_leveldb_tokens()
            tok = None
            for c in cands:
                if _token_alive(c):
                    tok = c
                    break
            if tok:
                STATE["config"]["token"] = tok
                save_config()
                log("已从本机浏览器自动导入 token")
                self._json({"ok": True})
            else:
                self._json({"ok": False, "stale": bool(cands)})
        else:
            self._send(404, "not found", "text/plain")

    def do_POST(self):
        path = self.path.split("?")[0]
        if not self._origin_ok():
            self._send(403, "forbidden origin", "text/plain")
            return
        try:
            body = self._body()
        except Exception:
            body = {}
        if path == "/api/config":
            allowed = set(DEFAULT_CONFIG.keys())
            for k, v in body.items():
                if k in allowed:
                    STATE["config"][k] = v
            save_config()
            self._json({"ok": True})
        elif path == "/api/control":
            act = body.get("action")
            if act == "start" and not STATE["running"]:
                # 每次启动用新的 Event: 上一轮残留的 stop.set 不会误杀新循环
                STATE["stop"] = threading.Event()
                STATE["running"] = True
                threading.Thread(target=monitor_loop,
                                 args=(STATE["stop"],), daemon=True).start()
            elif act == "stop":
                STATE["running"] = False
                STATE["stop"].set()
                with LOCK:
                    STATE["alert_msg"] = ""
                    STATE["alert_kinds"] = {}
            self._json({"ok": True, "running": STATE["running"]})
        elif path == "/api/clone":
            cfg = STATE["config"]
            mid = (body.get("machine_id") or "").strip()
            uuid = (cfg.get("clone_source_uuid") or "").strip()
            if not mid or not uuid:
                self._json({"ok": False, "msg": "缺少 machine_id 或未选择克隆源实例"})
                return
            try:
                src = clone_source_info(uuid, ttl=5)
                if src["status"] != "shutdown":
                    self._json({"ok": False, "msg": "源实例未关机 (%s)" % src["status"]})
                    return
                cand = next((x for x in STATE["candidates"] if x["machine_id"] == mid), None)
                if cand and not clone_compatible(src, cand):
                    self._json({"ok": False, "msg": "目标机器架构/CUDA 与源实例不兼容"})
                    return
                resp = clone_instance(mid, cfg.get("req_gpu_amount") or 1, cfg, src)
                if resp.get("code") == "Success":
                    STATE["config"]["created_count"] = cfg.get("created_count", 0) + 1
                    save_config()
                    with LOCK:
                        STATE["create_result"] = {"ok": True, "resp": resp.get("data"),
                                                  "time": time.strftime("%H:%M:%S"),
                                                  "ts": time.time(), "kind": "克隆"}
                    log("*** 手动克隆成功 -> %s (%s) ***" % (mid, src["uuid"]))
                    fire_webhook("AutoDL克隆成功 %s" % src["uuid"])
                    self._json({"ok": True, "data": resp.get("data")})
                else:
                    log("手动克隆失败(%s): %s" % (mid, resp.get("msg") or resp.get("code")))
                    self._json({"ok": False, "msg": resp.get("msg") or resp.get("code")})
            except Exception as e:
                self._json({"ok": False, "msg": str(e)})
        elif path == "/api/power_on":
            u = (body.get("instance_uuid") or "").strip()
            if not u:
                self._json({"ok": False, "msg": "缺少 instance_uuid"})
                return
            try:
                r = power_on(u)
                if r.get("code") == "Success":
                    log("手动开机成功: %s" % u)
                    self._json({"ok": True})
                else:
                    self._json({"ok": False, "msg": r.get("msg") or r.get("code")})
            except Exception as e:
                self._json({"ok": False, "msg": str(e)})
        else:
            self._send(404, "not found", "text/plain")


if __name__ == "__main__":
    # Windows 下 SO_REUSEADDR 允许重复绑定同端口, 多开会互相覆盖配置, 启动前探测
    try:
        urllib.request.urlopen("http://127.0.0.1:%d/api/state" % PORT, timeout=2)
        print("已有监控进程在运行 (http://127.0.0.1:%d), 请勿重复启动" % PORT)
        raise SystemExit(1)
    except SystemExit:
        raise
    except Exception:
        pass
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print("AutoDL 监控面板: http://127.0.0.1:%d" % PORT)
    threading.Thread(target=token_rescue_loop, daemon=True).start()
    threading.Timer(0.5, lambda: webbrowser.open("http://127.0.0.1:%d" % PORT)).start()
    srv.serve_forever()
