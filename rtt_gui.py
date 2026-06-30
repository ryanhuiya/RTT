import tkinter as tk
from tkinter import filedialog, scrolledtext, messagebox
import threading
import queue
import ctypes
import re
import os
import time

if os.name == "nt":
    import winreg
else:
    winreg = None

ConnectHelper = None
Session = None
PYOCD_IMPORT_ERROR = None


def load_pyocd():
    """延迟加载 pyOCD，让单文件 exe 先把窗口弹出来。"""
    global ConnectHelper, Session, PYOCD_IMPORT_ERROR

    if ConnectHelper is not None and Session is not None:
        return True

    try:
        from pyocd.core.helpers import ConnectHelper as _ConnectHelper
        from pyocd.core.session import Session as _Session
    except Exception as e:
        PYOCD_IMPORT_ERROR = e
        return False

    ConnectHelper = _ConnectHelper
    Session = _Session
    PYOCD_IMPORT_ERROR = None
    return True


def enable_high_dpi_awareness():
    """让 Windows 上的 Tk 窗口按真实 DPI 渲染，避免高分屏发糊。"""
    if os.name != "nt":
        return

    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        return
    except Exception:
        pass

    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


def configure_tk_scaling(root):
    """根据当前屏幕 DPI 调整 Tk 缩放，让控件和字体更清晰自然。"""
    try:
        dpi = root.winfo_fpixels("1i")
        if dpi > 0:
            root.tk.call("tk", "scaling", dpi / 72.0)
    except Exception:
        pass


enable_high_dpi_awareness()

# ==========================================
#   手写 RTT 驱动 (不依赖 pyocd RTT 库)
# ==========================================
class ManualRTT:
    MAX_RTT_BUFFER_SIZE = 65536
    MAX_WRITE_CHUNK = 64
    WRITE_RETRY_INTERVAL = 0.005
    WRITE_IDLE_TIMEOUT = 1.0

    def __init__(self, target, addr):
        self.target = target
        self.base_addr = addr
        self.up_buf = None   # {addr, size, rd_off_addr, wr_off_addr}
        self.down_buf = None # {addr, size, rd_off_addr, wr_off_addr}

    def is_likely_ram_address(self, addr):
        # 常见 Cortex-M/STM32 RAM 窗口，覆盖 CCM/DTCM/AXI/D2/D3 SRAM。
        return 0x10000000 <= addr < 0x40000000

    def validate_buf(self, name, buf):
        ptr = buf['ptr']
        size = buf['size']

        if ptr == 0:
            raise RuntimeError(f"{name} buffer ptr 为 0")
        if size <= 0 or size > self.MAX_RTT_BUFFER_SIZE:
            raise RuntimeError(f"{name} buffer size 异常: {size}")
        if not self.is_likely_ram_address(ptr):
            raise RuntimeError(f"{name} buffer ptr 不像 RAM 地址: 0x{ptr:08X}")

    def validate_offsets(self, name, buf, wr_off, rd_off):
        size = buf['size']
        ptr = buf['ptr']

        if wr_off >= size or rd_off >= size:
            raise RuntimeError(
                f"{name} 指针越界: wr_off={wr_off}, rd_off={rd_off}, size={size}, ptr=0x{ptr:08X}"
            )

    def drop_existing_up_data(self):
        """连接成功后丢弃连接前积压的 RTT UpBuffer 数据。"""
        if not self.up_buf:
            return

        self.validate_buf("RTT UpBuffer", self.up_buf)
        wr_off = self.target.read32(self.up_buf['wr_off_addr'])
        rd_off = self.target.read32(self.up_buf['rd_off_addr'])
        self.validate_offsets("RTT UpBuffer", self.up_buf, wr_off, rd_off)
        self.target.write32(self.up_buf['rd_off_addr'], wr_off)

    def init(self):
        """初始化：从内存读取控制块结构"""
        try:
            # 1. 验证 ID
            cid = self.target.read_memory_block8(self.base_addr, 16)
            cid_str = bytes(cid).decode('utf-8', errors='ignore')

            if "SEGGER RTT" not in cid_str:
                raise RuntimeError("地址处未发现 SEGGER RTT 签名")

            # 2. 读取并校验 Up/Down buffer 数量，避免描述符地址算飞。
            max_up = self.target.read32(self.base_addr + 16)
            max_down = self.target.read32(self.base_addr + 20)

            if max_up <= 0 or max_up > 8:
                raise RuntimeError(f"RTT MaxNumUpBuffers 异常: {max_up}")

            if max_down <= 0 or max_down > 8:
                raise RuntimeError(f"RTT MaxNumDownBuffers 异常: {max_down}")

            # 3. UpBuffer[0] 描述符地址
            ub_desc = self.base_addr + 24

            self.up_buf = {
                'ptr': self.target.read32(ub_desc + 4),
                'size': self.target.read32(ub_desc + 8),
                'wr_off_addr': ub_desc + 12,
                'rd_off_addr': ub_desc + 16
            }

            # 4. DownBuffer[0] 描述符地址
            db_desc = self.base_addr + 24 + (max_up * 24)

            self.down_buf = {
                'ptr': self.target.read32(db_desc + 4),
                'size': self.target.read32(db_desc + 8),
                'wr_off_addr': db_desc + 12,
                'rd_off_addr': db_desc + 16
            }

            # 5. 校验 buffer 参数
            self.validate_buf("RTT UpBuffer", self.up_buf)
            self.validate_buf("RTT DownBuffer", self.down_buf)
            return True
        except Exception as e:
            print(f"RTT Init Failed: {e}")
            return False

    def read(self):
        """读取数据 (Up Buffer: Target -> Host)。"""
        if not self.up_buf:
            return b""

        ptr = self.up_buf['ptr']
        size = self.up_buf['size']
        self.validate_buf("RTT UpBuffer", self.up_buf)

        # 1. 读取读写指针
        wr_off = self.target.read32(self.up_buf['wr_off_addr'])
        rd_off = self.target.read32(self.up_buf['rd_off_addr'])
        self.validate_offsets("RTT UpBuffer", self.up_buf, wr_off, rd_off)

        if wr_off == rd_off:
            return b"" # 无数据

        chunks = []
        if wr_off > rd_off:
            read_len = wr_off - rd_off
            if read_len <= 0 or read_len > size:
                raise RuntimeError(
                    f"RTT 读取长度异常: len={read_len}, wr={wr_off}, rd={rd_off}, size={size}"
                )
            raw = self.target.read_memory_block8(ptr + rd_off, read_len)
            chunks.append(bytes(raw))
            new_rd_off = wr_off
        else:
            first_len = size - rd_off
            second_len = wr_off
            if first_len < 0 or second_len < 0 or first_len + second_len > size:
                raise RuntimeError(
                    f"RTT 回绕读取长度异常: first={first_len}, second={second_len}, wr={wr_off}, rd={rd_off}, size={size}"
                )
            if first_len > 0:
                raw = self.target.read_memory_block8(ptr + rd_off, first_len)
                chunks.append(bytes(raw))
            if second_len > 0:
                raw = self.target.read_memory_block8(ptr, second_len)
                chunks.append(bytes(raw))
            new_rd_off = wr_off

        self.target.write32(self.up_buf['rd_off_addr'], new_rd_off)
        return b"".join(chunks)

    def write(self, data_bytes):
        """写入数据 (Down Buffer: Host -> Target)，缓冲区小时分段等待发送。"""
        if not self.down_buf or not data_bytes:
            return

        self.validate_buf("RTT DownBuffer", self.down_buf)
        size = self.down_buf['size']
        ptr = self.down_buf['ptr']
        total_len = len(data_bytes)
        sent_len = 0
        idle_deadline = time.monotonic() + self.WRITE_IDLE_TIMEOUT

        while sent_len < total_len:
            wr_off = self.target.read32(self.down_buf['wr_off_addr'])
            rd_off = self.target.read32(self.down_buf['rd_off_addr'])
            self.validate_offsets("RTT DownBuffer", self.down_buf, wr_off, rd_off)

            # 保留 1 字节避免与空缓冲区状态混淆。
            free_space = (rd_off - wr_off - 1 + size) % size
            if free_space <= 0:
                if time.monotonic() >= idle_deadline:
                    raise BufferError(
                        f"RTT 下行缓冲区等待超时: 已发送 {sent_len}/{total_len} 字节，当前剩余 0 字节"
                    )
                time.sleep(self.WRITE_RETRY_INTERVAL)
                continue

            chunk_len = min(total_len - sent_len, free_space, self.MAX_WRITE_CHUNK)
            first_len = min(chunk_len, size - wr_off)
            first_chunk = data_bytes[sent_len:sent_len + first_len]
            second_len = chunk_len - first_len
            second_chunk = data_bytes[sent_len + first_len:sent_len + chunk_len]

            self.write_block(ptr + wr_off, first_chunk)
            if second_len > 0:
                self.write_block(ptr, second_chunk)

            wr_off = (wr_off + chunk_len) % size
            self.target.write32(self.down_buf['wr_off_addr'], wr_off)
            sent_len += chunk_len
            idle_deadline = time.monotonic() + self.WRITE_IDLE_TIMEOUT

    def write_block(self, addr, data_bytes):
        if not data_bytes:
            return
        if hasattr(self.target, "write_memory_block8"):
            self.target.write_memory_block8(addr, list(data_bytes))
            return
        for byte_val in data_bytes:
            self.target.write8(addr, byte_val)
            addr += 1


# ==========================================
#   GUI 主程序
# ==========================================
class RTT_GUI:
    DEFAULT_TARGET_TYPE = "cortex_m"
    PROBE_RELEASE_COOLDOWN = 0.15
    AUTO_RECONNECT_DELAY = 1.0
    MAX_READ_ERRORS = 100
    READ_ERROR_LOG_INTERVAL = 20
    RX_IDLE_SLEEP = 0.01
    RX_ERROR_SLEEP = 0.05

    def __init__(self, root):
        self.root = root
        configure_tk_scaling(self.root)
        self.root.title("STM32 RTT 调试助手 V3.0(XJTU Robocon)")
        self.root.geometry("1320x780")
        self.root.minsize(1200, 680)
        self.settings_key = r"Software\XJTURC\Robocon_RTT_Tool"

        # UI: 配置区
        config_frame = tk.Frame(root)
        config_frame.pack(fill=tk.X, padx=5, pady=5)

        tk.Label(config_frame, text="符号文件:").pack(side=tk.LEFT)
        self.path_var = tk.StringVar(value=self.get_initial_map_path())

        self.path_entry = tk.Entry(config_frame, textvariable=self.path_var, width=50)
        self.path_entry.pack(side=tk.LEFT, padx=5)

        self.browse_btn = tk.Button(config_frame, text="浏览...", command=self.browse_file)
        self.browse_btn.pack(side=tk.LEFT)
        tk.Label(config_frame, text="Target:").pack(side=tk.LEFT, padx=(8, 0))
        self.target_var = tk.StringVar(value=self.load_saved_target_type())
        self.target_entry = tk.Entry(config_frame, textvariable=self.target_var, width=16)
        self.target_entry.pack(side=tk.LEFT, padx=5)
        self.connect_btn = tk.Button(config_frame, text="连接 RTT", command=self.start_rtt_thread, bg="#dddddd")
        self.connect_btn.pack(side=tk.LEFT, padx=10)
        self.disconnect_btn = tk.Button(config_frame, text="断开 RTT", command=self.disconnect_rtt, width=10,
                                        state="disabled", bg="#d9534f", fg="#fff4e8",
                                        activeforeground="#fff4e8", disabledforeground="#f2c7c5")
        self.disconnect_btn.pack(side=tk.LEFT)
        self.clear_btn = tk.Button(config_frame, text="Clear", command=self.clear_log_area, width=8,
                                   bg="#2f3a45", fg="#dbe7f3", activeforeground="#ffffff")
        self.clear_btn.pack(side=tk.LEFT, padx=(8, 0))
        self.status_var = tk.StringVar(value="未连接")
        self.status_label = tk.Label(config_frame, textvariable=self.status_var, fg="#666666")
        self.status_label.pack(side=tk.LEFT, padx=(12, 0))

        # UI: 日志区
        self.log_area = scrolledtext.ScrolledText(root, state='disabled', bg="black", fg="#00FF00", font=("Consolas", 10))
        self.log_area.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)
        self.log_area.tag_config("sys", foreground="yellow")
        self.log_area.tag_config("tx", foreground="white")
        self.log_area.tag_config("rx", foreground="#00FF00")
        self.log_area.tag_config("time", foreground="#7aa2c8", font=("Consolas", 9))
        self.log_line_start = True

        # UI: 发送区
        send_frame = tk.Frame(root)
        send_frame.pack(fill=tk.X, padx=5, pady=5)
        self.input_entry = tk.Entry(send_frame, font=("Consolas", 11))
        self.input_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.input_entry.bind("<Return>", self.send_command)
        self.send_btn = tk.Button(send_frame, text="发送", command=self.send_command, width=10, bg="#4CAF50", fg="white")
        self.send_btn.pack(side=tk.RIGHT, padx=5)

        self.session = None
        self.rtt = None
        self.running = False
        self.connecting = False
        self.disconnecting = False
        self.cleanup_in_progress = False
        self.pending_reconnect = False
        self.pending_reconnect_path = ""
        self.pending_reconnect_delay = None
        self.cleanup_token = 0
        self.closing = False
        self.io_thread = None
        self.opening_session = None
        self.opening_probe = None
        self.send_queue = queue.Queue()
        self.ui_queue = queue.Queue()
        self.stop_event = threading.Event()
        self.pyocd_preload_started = False

        self.set_disconnected_ui()
        self.root.after(50, self.process_ui_queue)
        self.root.after(50, self.preload_pyocd)

    def preload_pyocd(self):
        if self.pyocd_preload_started:
            return

        self.pyocd_preload_started = True
        threading.Thread(target=load_pyocd, daemon=True).start()

    def find_default_symbol_file(self):
        for d in ["build/Debug", "cmake-build-debug", "."]:
            if os.path.exists(d):
                for f in os.listdir(d):
                    if f.lower().endswith((".map", ".elf")):
                        return os.path.join(d, f)
        return ""

    def load_saved_map_path(self):
        if winreg is None:
            return ""

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, self.settings_key) as key:
                value, _ = winreg.QueryValueEx(key, "last_map_path")
        except OSError:
            return ""

        return value.strip() if isinstance(value, str) else ""

    def load_saved_target_type(self):
        if winreg is None:
            return self.DEFAULT_TARGET_TYPE

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, self.settings_key) as key:
                value, _ = winreg.QueryValueEx(key, "target_type")
        except OSError:
            return self.DEFAULT_TARGET_TYPE

        if not isinstance(value, str):
            return self.DEFAULT_TARGET_TYPE

        saved_target = value.strip()
        if saved_target.lower() in ("", "auto", "自动", "stm32g474retx"):
            return self.DEFAULT_TARGET_TYPE
        return saved_target

    def get_initial_map_path(self):
        saved_path = self.load_saved_map_path()
        if saved_path and os.path.exists(saved_path):
            return saved_path
        return self.find_default_symbol_file()

    def save_map_path(self, path=None):
        map_path = (path if path is not None else self.path_var.get()).strip()
        if not map_path or winreg is None:
            return

        try:
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, self.settings_key) as key:
                winreg.SetValueEx(key, "last_map_path", 0, winreg.REG_SZ, map_path)
        except OSError as e:
            self.log(f">>> 保存配置失败: {e}", "sys")

    def save_target_type(self, target_type=None):
        if winreg is None:
            return

        target_type = (target_type if target_type is not None else self.target_var.get()).strip()
        try:
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, self.settings_key) as key:
                winreg.SetValueEx(key, "target_type", 0, winreg.REG_SZ, target_type)
        except OSError as e:
            self.log(f">>> 保存 Target 配置失败: {e}", "sys")

    def browse_file(self):
        f = filedialog.askopenfilename(filetypes=[
            ("Map/ELF Files", "*.map *.elf"),
            ("Map Files", "*.map"),
            ("ELF Files", "*.elf"),
            ("All Files", "*.*"),
        ])
        if f:
            self.path_var.set(f)
            self.save_map_path(path=f)

    def get_rtt_address(self, symbol_file):
        ext = os.path.splitext(symbol_file)[1].lower()
        if ext == ".elf":
            return self.get_rtt_address_from_elf(symbol_file)
        return self.get_rtt_address_from_map(symbol_file)

    def get_rtt_address_from_map(self, map_file):
        try:
            with open(map_file, 'r', encoding='utf-8', errors='ignore') as f:
                content = f.read()
                for symbol_name in ("_SEGGER_RTT", "SEGGER_RTT"):
                    match = re.search(rf'(0x[0-9A-Fa-f]+)\s+{re.escape(symbol_name)}\b', content)
                    if match:
                        return match.group(1)
                    match = re.search(rf'\b{re.escape(symbol_name)}\b\s+(0x[0-9A-Fa-f]+)', content)
                    if match:
                        return match.group(1)
        except Exception as e:
            self.log(f">>> 解析 Map 失败: {e}", "sys")
        return None

    def get_rtt_address_from_elf(self, elf_file):
        try:
            from elftools.elf.elffile import ELFFile

            with open(elf_file, "rb") as f:
                elf = ELFFile(f)
                for section_name in (".symtab", ".dynsym"):
                    symtab = elf.get_section_by_name(section_name)
                    if not symtab:
                        continue
                    for symbol_name in ("_SEGGER_RTT", "SEGGER_RTT"):
                        symbols = symtab.get_symbol_by_name(symbol_name)
                        if symbols:
                            return f"0x{symbols[0]['st_value']:X}"
        except Exception as e:
            self.log(f">>> 解析 ELF 失败: {e}", "sys")
        return None

    def start_rtt_thread(self):
        if self.running or self.connecting:
            return
        map_path = self.path_var.get().strip()
        if self.io_thread and self.io_thread.is_alive():
            self.queue_reconnect(map_path)
            self.log(">>> 上一次连接操作还在退出，结束后将自动重连。", "sys")
            return
        if self.cleanup_in_progress:
            self.queue_reconnect(map_path)
            self.log(">>> 正在释放探针资源，完成后将自动重连。", "sys")
            return
        self.begin_connect(map_path)

    def begin_connect(self, map_path):
        self.save_map_path(map_path)
        self.save_target_type()
        self.stop_event.clear()
        self.clear_send_queue()
        self.connecting = True
        self.disconnecting = False
        self.set_connecting_ui()
        self.io_thread = threading.Thread(target=self.connect_rtt, args=(map_path,), daemon=True)
        self.io_thread.start()

    def start_pending_reconnect(self):
        if self.closing or not self.pending_reconnect:
            return
        if self.io_thread and self.io_thread.is_alive():
            try:
                self.root.after(100, self.start_pending_reconnect)
            except RuntimeError:
                pass
            return
        if self.running or self.connecting or self.cleanup_in_progress:
            return

        map_path = self.pending_reconnect_path or self.path_var.get().strip()
        self.pending_reconnect = False
        self.pending_reconnect_path = ""
        self.pending_reconnect_delay = None
        self.log(">>> 资源已释放，正在自动重连...", "sys")
        self.begin_connect(map_path)

    def finish_cleanup_cooldown(self, token):
        if token != self.cleanup_token:
            return

        self.cleanup_in_progress = False
        if self.pending_reconnect and not self.closing:
            self.ui_call(self.start_pending_reconnect)

    def start_cleanup_cooldown(self, delay=None):
        if delay is None and self.pending_reconnect_delay is not None:
            delay = self.pending_reconnect_delay
            self.pending_reconnect_delay = None
        delay = self.PROBE_RELEASE_COOLDOWN if delay is None else delay
        self.cleanup_token += 1
        token = self.cleanup_token
        self.cleanup_in_progress = True

        def worker():
            time.sleep(delay)
            self.finish_cleanup_cooldown(token)

        threading.Thread(target=worker, daemon=True).start()

    def format_exception(self, exc):
        text = str(exc).strip()
        return text if text else exc.__class__.__name__

    def set_status(self, text):
        self.status_var.set(text)

    def queue_reconnect(self, map_path, delay=None):
        self.pending_reconnect = True
        self.pending_reconnect_path = map_path
        self.pending_reconnect_delay = delay

    def release_probe_now(self, session=None, probe=None):
        probe = probe or getattr(session, "probe", None)
        if not probe:
            return

        try:
            is_open = getattr(probe, "is_open", False)
        except Exception:
            is_open = True

        if not is_open:
            return

        try:
            probe.close()
        except Exception:
            pass

    def close_session_async(self, session, clear_cleanup_flag=False, quiet=True):
        if not session:
            if clear_cleanup_flag:
                self.cleanup_in_progress = False
            return

        threading.Thread(
            target=self.close_session,
            args=(session, clear_cleanup_flag, quiet),
            daemon=True,
        ).start()

    def refresh_probe(self, probe):
        probe_uid = getattr(probe, "unique_id", "")
        try:
            probes = ConnectHelper.get_all_connected_probes(
                blocking=False,
                print_wait_message=False,
            )
        except Exception:
            return probe

        if not probes:
            return probe

        if probe_uid:
            for candidate in probes:
                if getattr(candidate, "unique_id", "") == probe_uid:
                    return candidate

        return probes[0]

    def disconnect_rtt(self):
        if self.disconnecting or (not self.connecting and not self.running and not self.session):
            return

        if self.connecting and self.session is None:
            self.stop_event.set()
            self.clear_send_queue()
            opening_session = self.opening_session
            opening_probe = self.opening_probe
            self.release_probe_now(session=opening_session, probe=opening_probe)
            self.close_session_async(opening_session, quiet=True)
            self.start_cleanup_cooldown()
            self.connecting = False
            self.disconnecting = False
            self.log(">>> 已取消连接请求。", "sys")
            self.ui_call(self.set_disconnected_ui)
            return

        self.stop_event.set()
        self.clear_send_queue()
        session_to_close = self.session

        # 先在界面层立刻切回“未连接”，避免等待底层 close() 完成。
        self.running = False
        self.connecting = False
        self.disconnecting = False
        self.session = None
        self.rtt = None
        if session_to_close:
            self.release_probe_now(session=session_to_close)
            self.start_cleanup_cooldown()
        self.set_disconnected_ui()
        self.log(">>> RTT 已断开。", "sys")

        if session_to_close:
            self.close_session_async(session_to_close, quiet=True)

    def build_target_candidates(self, probe):
        candidates = []
        requested_target = self.target_var.get().strip()
        if requested_target.lower() in ("auto", "自动"):
            requested_target = ""
        if requested_target:
            if requested_target.lower() == self.DEFAULT_TARGET_TYPE:
                source = "默认"
            else:
                source = "用户指定"
            candidates.append((requested_target, source))
            return candidates
        else:
            board_info = getattr(probe, "associated_board_info", None)
            detected_target = getattr(board_info, "target", None) if board_info else None
            if detected_target:
                candidates.append((detected_target, "探针识别"))

        if not any(target == "cortex_m" for target, _ in candidates):
            candidates.append(("cortex_m", "通用 fallback"))
        return candidates

    def build_session_options(self):
        return {
            "connect_mode": "attach",
            "resume_on_disconnect": False,
        }

    def open_session_with_fallback(self, probe):
        last_error = None
        candidates = self.build_target_candidates(probe)
        for index, (target_name, source) in enumerate(candidates):
            session = None
            if index:
                probe = self.refresh_probe(probe)
            try:
                session = Session(
                    probe,
                    auto_open=False,
                    options=self.build_session_options(),
                    target_override=target_name,
                )
                self.log(f">>> Target: {target_name} ({source})", "sys")
                self.opening_session = session
                self.opening_probe = probe
                session.open()
                return session
            except Exception as e:
                last_error = e
                self.release_probe_now(session=session, probe=probe)
                self.close_session_async(session, quiet=True)
                if target_name != "cortex_m":
                    detail = self.format_exception(e)
                    if index + 1 < len(candidates):
                        self.log(f">>> Target {target_name} 不可用，尝试 fallback: {detail}", "sys")
                    else:
                        self.log(f">>> Target {target_name} 不可用: {detail}", "sys")
                if index + 1 < len(candidates):
                    time.sleep(0.05)
            finally:
                if self.opening_session is session:
                    self.opening_session = None
                    self.opening_probe = None

        raise last_error if last_error else RuntimeError("无法打开 pyOCD Session")

    def connect_rtt(self, map_path):
        if not load_pyocd():
            detail = f"\n{PYOCD_IMPORT_ERROR}" if PYOCD_IMPORT_ERROR else ""
            self.log(f">>> 错误: 未检测到 pyOCD 库，请安装 pyocd。{detail}", "sys")
            self.connecting = False
            self.ui_call(self.set_disconnected_ui)
            return

        if not os.path.exists(map_path):
            self.log(">>> 错误: 符号文件不存在", "sys")
            self.connecting = False
            self.ui_call(self.set_disconnected_ui)
            return

        addr_str = self.get_rtt_address(map_path)
        if not addr_str:
            self.log(">>> 错误: 未找到 _SEGGER_RTT 地址", "sys")
            self.connecting = False
            self.ui_call(self.set_disconnected_ui)
            return

        rtt_addr = int(addr_str, 16)
        self.log(f">>> 目标地址: {hex(rtt_addr)}", "sys")
        self.ui_call(self.set_status, "搜索调试器...")
        self.log(">>> 正在搜索调试器...", "sys")
        connect_failed = False
        link_lost = False

        try:
            # 非阻塞查找探针。没插硬件时立即返回，不进入 pyOCD 的无限等待模式。
            probes = ConnectHelper.get_all_connected_probes(
                blocking=False,
                print_wait_message=False,
            )
            if not probes:
                if self.stop_event.is_set():
                    return
                self.log(">>> 未检测到调试器，请连接硬件后重试。", "sys")
                return

            if len(probes) > 1:
                first_probe = probes[0]
                self.log(
                    f">>> 检测到多个调试器，默认使用第一个: {first_probe.description} ({first_probe.unique_id})",
                    "sys"
                )

            if self.stop_event.is_set():
                return

            self.ui_call(self.set_status, "连接芯片...")
            self.log(">>> 已检测到调试器，正在连接芯片...", "sys")
            self.session = self.open_session_with_fallback(probes[0])
            if self.stop_event.is_set():
                return

            # === 使用手动 RTT 驱动 ===
            self.rtt = ManualRTT(self.session.target, rtt_addr)
            if self.rtt.init():
                self.rtt.drop_existing_up_data()
                self.running = True
                self.connecting = False
                self.ui_call(self.set_connected_ui)
                self.log(">>> 连接成功！已丢弃连接前积压 RTT 数据。", "sys")
                link_lost = self.io_loop() == "link_lost"
                if link_lost and not self.closing:
                    self.queue_reconnect(map_path, self.AUTO_RECONNECT_DELAY)
                    self.ui_call(self.set_status, "链路断开，准备重连...")
                    self.log(f">>> 链路异常中断，{self.AUTO_RECONNECT_DELAY:.1f}s 后自动重连。", "sys")
            else:
                self.log(">>> RTT 初始化失败，请检查 Map 文件是否匹配。", "sys")
                self.release_probe_now(session=self.session)

        except Exception as e:
            connect_failed = True
            self.running = False
            if not self.stop_event.is_set():
                self.log(f">>> 连接异常: {self.format_exception(e)}", "sys")
        finally:
            self.finish_disconnect()
            if connect_failed and not self.closing and not link_lost:
                self.start_cleanup_cooldown()

    def io_loop(self):
        consecutive_read_errors = 0

        while self.running and self.session and self.session.is_open and not self.stop_event.is_set():
            try:
                self.flush_send_queue()
                if self.stop_event.is_set():
                    break

                # 读取数据
                try:
                    data = self.rtt.read()
                    if consecutive_read_errors:
                        self.log(">>> 读取已恢复。", "sys")
                    consecutive_read_errors = 0
                    if data:
                        text = data.decode('utf-8', errors='replace')
                        self.ui_call(self.append_log, text, "rx")
                except Exception as e:
                    if self.stop_event.is_set():
                        break

                    consecutive_read_errors += 1
                    if consecutive_read_errors == 1:
                        self.log(f">>> 读取暂时失败，正在重试: {self.format_exception(e)}", "sys")
                    elif consecutive_read_errors >= self.MAX_READ_ERRORS:
                        self.log(
                            f">>> 连续读取失败 {consecutive_read_errors} 次，已断开: {self.format_exception(e)}",
                            "sys"
                        )
                        return "link_lost"
                    elif consecutive_read_errors % self.READ_ERROR_LOG_INTERVAL == 0:
                        self.log(f">>> 读取仍在重试: {consecutive_read_errors}/{self.MAX_READ_ERRORS}", "sys")

                    time.sleep(self.RX_ERROR_SLEEP)
                    continue

                if not data:
                    time.sleep(self.RX_IDLE_SLEEP)
            except Exception as e:
                if not self.stop_event.is_set():
                    self.log(f">>> 读取中断: {self.format_exception(e)}", "sys")
                    return "link_lost"
                break

        return "stopped"

    def flush_send_queue(self):
        while self.running and not self.stop_event.is_set():
            try:
                data = self.send_queue.get_nowait()
            except queue.Empty:
                return

            try:
                self.rtt.write(data)
            except BufferError as e:
                if not self.stop_event.is_set():
                    self.log(f"[发送失败: {e}]", "sys")
                self.clear_send_queue()
                break
            except Exception as e:
                if not self.stop_event.is_set():
                    self.log(f"[发送失败: {e}]", "sys")
                self.stop_event.set()
                break

    def append_timestamp(self):
        self.log_area.insert(tk.END, f"[{time.strftime('%H:%M:%S')}] ", "time")

    def append_log(self, text, tag="rx"):
        self.log_area.config(state='normal')
        for part in text.splitlines(keepends=True):
            if self.log_line_start:
                self.append_timestamp()
            self.log_area.insert(tk.END, part, tag)
            self.log_line_start = part.endswith(("\n", "\r"))
        self.log_area.see(tk.END)
        self.log_area.config(state='disabled')

    def log(self, text, tag="rx"):
        self.ui_call(self.append_log, text + "\n", tag)

    def clear_log_area(self):
        self.log_area.config(state='normal')
        self.log_area.delete("1.0", tk.END)
        self.log_area.config(state='disabled')
        self.log_line_start = True

    def ui_call(self, func, *args):
        self.ui_queue.put((func, args))

    def process_ui_queue(self):
        while True:
            try:
                func, args = self.ui_queue.get_nowait()
            except queue.Empty:
                break
            try:
                func(*args)
            except tk.TclError:
                pass

        try:
            self.root.after(50, self.process_ui_queue)
        except RuntimeError:
            pass

    def clear_send_queue(self):
        while True:
            try:
                self.send_queue.get_nowait()
            except queue.Empty:
                return

    def close_session(self, session=None, clear_cleanup_flag=False, quiet=False):
        session = session if session is not None else self.session
        if not session:
            if clear_cleanup_flag:
                self.cleanup_in_progress = False
                if self.pending_reconnect and not self.closing:
                    self.ui_call(self.start_pending_reconnect)
            return
        try:
            session.close()
        except Exception as e:
            if not quiet and not self.closing:
                self.log(f">>> 关闭连接时出现异常: {self.format_exception(e)}", "sys")
        finally:
            if clear_cleanup_flag:
                self.cleanup_in_progress = False
                if self.pending_reconnect and not self.closing:
                    self.ui_call(self.start_pending_reconnect)

    def finish_disconnect(self):
        self.running = False
        self.connecting = False
        self.disconnecting = False
        self.stop_event.set()
        self.clear_send_queue()
        session_to_close = self.session
        if session_to_close:
            self.release_probe_now(session=session_to_close)
            self.start_cleanup_cooldown()
            self.close_session_async(session_to_close, quiet=True)
        self.session = None
        self.rtt = None
        if not self.closing:
            self.ui_call(self.set_disconnected_ui)
            if self.pending_reconnect:
                self.ui_call(self.set_status, "等待自动重连...")

    def set_disconnected_ui(self):
        self.set_status("未连接")
        self.path_entry.config(state="normal")
        self.browse_btn.config(state="normal")
        self.target_entry.config(state="normal")
        self.connect_btn.config(text="连接 RTT", state="normal")
        self.disconnect_btn.config(text="断开 RTT", state="disabled")
        self.input_entry.config(state="disabled")
        self.send_btn.config(state="disabled")

    def set_connecting_ui(self):
        self.set_status("连接中...")
        self.path_entry.config(state="disabled")
        self.browse_btn.config(state="disabled")
        self.target_entry.config(state="disabled")
        self.connect_btn.config(text="连接中...", state="disabled")
        self.disconnect_btn.config(text="取消连接", state="normal")
        self.input_entry.config(state="disabled")
        self.send_btn.config(state="disabled")

    def set_connected_ui(self):
        self.set_status("已连接")
        self.path_entry.config(state="disabled")
        self.browse_btn.config(state="disabled")
        self.target_entry.config(state="disabled")
        self.connect_btn.config(text="已连接", state="disabled")
        self.disconnect_btn.config(text="断开 RTT", state="normal")
        self.input_entry.config(state="normal")
        self.send_btn.config(state="normal")

    def set_disconnecting_ui(self):
        self.set_status("断开中...")
        self.path_entry.config(state="disabled")
        self.browse_btn.config(state="disabled")
        self.target_entry.config(state="disabled")
        self.connect_btn.config(text="连接 RTT", state="disabled")
        self.disconnect_btn.config(text="断开中...", state="disabled")
        self.input_entry.config(state="disabled")
        self.send_btn.config(state="disabled")

    def send_command(self, event=None):
        if not self.running or not self.rtt:
            messagebox.showwarning("警告", "请先连接！")
            return

        cmd = self.input_entry.get()
        if not cmd: return

        try:
            self.log(f"-> {cmd}", "tx")

            # 强制加上 \r\n 并转为 bytes
            data = (cmd + "\r\n").encode('utf-8')

            self.input_entry.delete(0, tk.END)
            self.send_queue.put(data)
        except Exception as e:
            self.log(f"[发送失败: {e}]", "sys")

    def on_closing(self):
        self.closing = True
        self.stop_event.set()
        self.clear_send_queue()
        self.pending_reconnect = False
        self.pending_reconnect_path = ""
        self.pending_reconnect_delay = None
        self.save_map_path()
        self.save_target_type()
        threading.Thread(target=self._shutdown, daemon=True).start()

    def _shutdown(self):
        session_to_close = self.session
        self.session = None
        self.release_probe_now(session=session_to_close)
        self.close_session(session_to_close, quiet=True)
        self.ui_call(self.root.destroy)

if __name__ == "__main__":
    root = tk.Tk()
    app = RTT_GUI(root)
    root.protocol("WM_DELETE_WINDOW", app.on_closing)
    root.mainloop()
