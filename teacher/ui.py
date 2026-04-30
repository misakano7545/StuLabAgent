"""tkinter UI: student list, logs, rename / IPv4 dialogs."""

from __future__ import annotations

import json
import os
import re
import sys
import tkinter as tk
import tkinter.font as tkfont
from tkinter import messagebox, scrolledtext, ttk
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Set, Tuple

from common.clash_config import build_clash_config, config_to_yaml

if TYPE_CHECKING:
    from teacher.server import ClientSession, TeacherServer

def _get_config_path() -> str:
    if getattr(sys, 'frozen', False):
        exe_dir = os.path.dirname(os.path.abspath(sys.executable))
    else:
        script_dir = os.path.dirname(os.path.abspath(__file__))
        exe_dir = os.path.dirname(script_dir)
    return os.path.join(exe_dir, "configs", "teacher.json")


def _load_config() -> Dict[str, Any]:
    config_path = _get_config_path()
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_config(config: Dict[str, Any]) -> None:
    config_path = _get_config_path()
    try:
        os.makedirs(os.path.dirname(config_path), exist_ok=True)
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"Save config error: {e}")


def _fmt_time(ts: float) -> str:
    if ts <= 0:
        return ""
    import time

    return time.strftime("%H:%M:%S", time.localtime(ts))


def _display_reported_mac(mid: str) -> str:
    """列表中展示上报网卡 MAC（优先取 machine_id 第一段 12 位十六进制）。"""
    m = (mid or "").strip()
    if not m:
        return "—"
    # machine_id 可能是 "mac1|mac2|..."，UI 只展示首个上报 MAC。
    first = m.split("|", 1)[0].strip()
    hx = re.sub(r"[^0-9A-Fa-f]", "", first)
    if len(hx) != 12:
        return first or "—"
    hx = hx.upper()
    return "-".join([hx[i : i + 2] for i in range(0, 12, 2)])


class TeacherApp(tk.Tk):
    def __init__(
        self,
        server: "TeacherServer",
        on_enqueue_rename: Callable[[str, str, str], bool],
        on_enqueue_set_ipv4: Callable[[str, str, Dict[str, Any]], bool],
        on_enqueue_power: Callable[[str, str, str], bool],
        on_enqueue_network_restrict: Callable[[str, str, Dict[str, Any]], bool],
    ) -> None:
        super().__init__()
        self.title("机房管理 — 教师端   by MisakaNo（QQ:1689910089）")
        self.geometry("1080x560")
        self._server = server
        self._on_enqueue_rename = on_enqueue_rename
        self._on_enqueue_set_ipv4 = on_enqueue_set_ipv4
        self._on_enqueue_power = on_enqueue_power
        self._on_enqueue_network_restrict = on_enqueue_network_restrict
        self._cmd_counter = 0
        self._cols: Tuple[str, ...] = ("hostname", "machine_id", "ipv4", "addr", "last_seen", "status", "os")
        self._headings: Dict[str, str] = {
            "hostname": "计算机名",
            "machine_id": "上报网卡 MAC",
            "ipv4": "上报 IPv4",
            "addr": "连接地址",
            "last_seen": "最后连接时间",
            "status": "状态",
            "os": "系统",
        }
        self._filter_arrow = " ▼"
        self._heading_font = tkfont.nametofont("TkHeadingFont")
        self._active_filters: Dict[str, Set[str]] = {}
        self._sessions_cache: List["ClientSession"] = []
        self._drag_select_anchor_iid: Optional[str] = None
        self._drag_select_anchor_x: Optional[int] = None
        self._drag_select_anchor_y: Optional[int] = None
        self._drag_select_active = False
        self._drag_visual: Optional[tk.Toplevel] = None
        self._build_menu()

        main = ttk.Frame(self, padding=6)
        main.pack(fill=tk.BOTH, expand=True)

        self.tree = ttk.Treeview(
            main,
            columns=self._cols,
            show="headings",
            height=12,
            selectmode="extended",
        )
        widths = {
            "hostname": 120,
            "machine_id": 130,
            "ipv4": 110,
            "addr": 100,
            "last_seen": 88,
            "status": 56,
            "os": 160,
        }
        for c in self._cols:
            self.tree.heading(c, text=self._headings[c])
            self.tree.column(c, width=widths[c], anchor=tk.W)
        self._update_heading_labels()

        vsb = ttk.Scrollbar(main, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        self.tree.bind("<ButtonPress-1>", self._on_tree_button_press, add="+")
        self.tree.bind("<B1-Motion>", self._on_tree_drag_motion, add="+")
        self.tree.bind("<ButtonRelease-1>", self._on_tree_button_release, add="+")
        self.tree.bind("<Button-3>", self._on_tree_right_click, add="+")

        self._context_menu = tk.Menu(self, tearoff=0)
        self._context_menu.add_command(label="修改计算机名", command=self._dlg_rename)
        self._context_menu.add_command(label="修改网卡IPV4", command=self._dlg_ipv4)
        self._context_menu.add_command(label="电源操作", command=self._dlg_power)
        self._context_menu.add_command(label="网络控制", command=self._dlg_network_restrict)

        btn_row = ttk.Frame(main)
        btn_row.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        ttk.Button(btn_row, text="修改计算机名…", command=self._dlg_rename).pack(
            side=tk.LEFT, padx=(0, 6)
        )
        ttk.Button(btn_row, text="修改网卡 IPv4…", command=self._dlg_ipv4).pack(
            side=tk.LEFT
        )
        ttk.Button(btn_row, text="电源操作…", command=self._dlg_power).pack(
            side=tk.LEFT, padx=(6, 0)
        )
        ttk.Button(btn_row, text="网络限制…", command=self._dlg_network_restrict).pack(
            side=tk.LEFT, padx=(6, 0)
        )

        ttk.Label(main, text="日志").grid(row=2, column=0, columnspan=2, sticky="w", pady=(8, 0))
        self.log = scrolledtext.ScrolledText(main, height=12, state=tk.DISABLED, wrap=tk.WORD)
        self.log.grid(row=3, column=0, columnspan=2, sticky="nsew", pady=(4, 0))

        main.rowconfigure(0, weight=1)
        main.rowconfigure(3, weight=1)
        main.columnconfigure(0, weight=1)

    def _build_menu(self) -> None:
        menubar = tk.Menu(self)
        help_menu = tk.Menu(menubar, tearoff=0)
        help_menu.add_command(label="关于", command=self._show_about_dialog)
        menubar.add_cascade(label="帮助", menu=help_menu)
        self.config(menu=menubar)

    def _show_about_dialog(self) -> None:
        messagebox.showinfo(
            "关于",
            (
                "机房管理 — 教师端\n"
                "Copyright © MisakaNo & Yulin Vocational Technical College\n\n"
                "功能：\n"
                "- 学生机在线状态与信息查看\n"
                "- 批量电源操作（关机/重启）\n"
                "- 计算机名与 IPv4 配置管理\n"
                "- 网络访问限制控制"
            ),
            parent=self,
        )

    def _on_tree_button_press(self, event: tk.Event) -> Optional[str]:
        region = self.tree.identify("region", event.x, event.y)
        if region == "heading":
            col = self.tree.identify_column(event.x)
            if self._is_filter_arrow_click(int(event.x), col):
                self._open_column_filter(col, int(event.x_root), int(event.y_root))
                return "break"
            return None
        clicked_iid = self.tree.identify_row(event.y)
        self._drag_select_anchor_iid = clicked_iid if clicked_iid else None
        self._drag_select_anchor_x = int(event.x)
        self._drag_select_anchor_y = int(event.y)
        self._drag_select_active = False
        if not clicked_iid:
            self.tree.selection_remove(self.tree.selection())
        return None

    def _on_tree_drag_motion(self, event: tk.Event) -> Optional[str]:
        if self._drag_select_anchor_y is None:
            return None
        height = max(1, int(self.tree.winfo_height()))
        y = int(event.y)
        if y < 0:
            self.tree.yview_scroll(-1, "units")
        elif y > height:
            self.tree.yview_scroll(1, "units")

        anchor_iid = self._drag_select_anchor_iid or self._nearest_tree_iid(self._drag_select_anchor_y)
        current_iid = self._nearest_tree_iid(y)
        if not anchor_iid or not current_iid:
            return None
        self._drag_select_anchor_iid = anchor_iid
        self._drag_select_active = True
        self._show_drag_visual(int(event.x), y)
        selected = self._iids_between(anchor_iid, current_iid)
        if selected:
            self.tree.selection_set(selected)
            self.tree.focus(current_iid)
        return "break"

    def _on_tree_button_release(self, _event: tk.Event) -> Optional[str]:
        had_drag = self._drag_select_active
        self._drag_select_anchor_iid = None
        self._drag_select_anchor_x = None
        self._drag_select_anchor_y = None
        self._drag_select_active = False
        self._hide_drag_visual()
        if had_drag:
            return "break"
        return None

    def _on_tree_right_click(self, event: tk.Event) -> Optional[str]:
        clicked_iid = self.tree.identify_row(event.y)
        if not clicked_iid:
            return None
        current_selection = set(self.tree.selection())
        if clicked_iid not in current_selection:
            self.tree.selection_set(clicked_iid)
        self.tree.focus(clicked_iid)
        try:
            self._context_menu.tk_popup(int(event.x_root), int(event.y_root))
        finally:
            self._context_menu.grab_release()
        return "break"

    def _show_drag_visual(self, current_x: int, current_y: int) -> None:
        if self._drag_select_anchor_x is None or self._drag_select_anchor_y is None:
            return
        if self._drag_visual is None:
            visual = tk.Toplevel(self)
            visual.overrideredirect(True)
            try:
                visual.attributes("-topmost", True)
                visual.attributes("-alpha", 0.22)
            except tk.TclError:
                pass
            visual.configure(bg="#3B82F6")
            self._drag_visual = visual

        tree_w = max(1, int(self.tree.winfo_width()))
        tree_h = max(1, int(self.tree.winfo_height()))
        x0 = max(0, min(tree_w, int(self._drag_select_anchor_x)))
        y0 = max(0, min(tree_h, int(self._drag_select_anchor_y)))
        x1 = max(0, min(tree_w, int(current_x)))
        y1 = max(0, min(tree_h, int(current_y)))
        left = min(x0, x1)
        top = min(y0, y1)
        width = max(1, abs(x1 - x0))
        height = max(1, abs(y1 - y0))
        screen_x = int(self.tree.winfo_rootx()) + left
        screen_y = int(self.tree.winfo_rooty()) + top

        self._drag_visual.geometry("%dx%d+%d+%d" % (width, height, screen_x, screen_y))
        self._drag_visual.deiconify()

    def _hide_drag_visual(self) -> None:
        if self._drag_visual is None:
            return
        self._drag_visual.destroy()
        self._drag_visual = None

    def _nearest_tree_iid(self, y: int) -> Optional[str]:
        iid = self.tree.identify_row(y)
        if iid:
            return iid
        children = self.tree.get_children("")
        if not children:
            return None
        if y <= 0:
            return str(children[0])
        return str(children[-1])

    def _iids_between(self, iid_a: str, iid_b: str) -> List[str]:
        children = list(self.tree.get_children(""))
        if iid_a not in children or iid_b not in children:
            return []
        idx_a = children.index(iid_a)
        idx_b = children.index(iid_b)
        lo = min(idx_a, idx_b)
        hi = max(idx_a, idx_b)
        return [str(iid) for iid in children[lo : hi + 1]]

    def log_line(self, text: str) -> None:
        self.log.configure(state=tk.NORMAL)
        self.log.insert(tk.END, text + "\n")
        self.log.see(tk.END)
        self.log.configure(state=tk.DISABLED)

    def next_cmd_id(self) -> str:
        self._cmd_counter += 1
        return "c%d" % self._cmd_counter

    def selected_session_id(self) -> Optional[str]:
        sel = self.tree.selection()
        if not sel:
            return None
        return str(sel[0])

    def selected_session_ids(self) -> List[str]:
        sel = self.tree.selection()
        return [str(sid) for sid in sel]

    def _pc_name_for_session(self, sid: str) -> str:
        for s in self._sessions_cache:
            if s.session_id == sid:
                h = (s.hostname or "").strip()
                return h if h else "-"
        return "-"

    def _session_values(self, s: "ClientSession") -> Tuple[str, str, str, str, str, str, str]:
        status = "在线" if s.online else "离线"
        return (
            s.hostname,
            _display_reported_mac(s.machine_id),
            s.reported_ipv4,
            s.addr,
            _fmt_time(s.last_seen),
            status,
            s.os_version[:40] if s.os_version else "",
        )

    def _column_key_from_tk(self, tk_col: str) -> Optional[str]:
        if not tk_col.startswith("#"):
            return None
        try:
            idx = int(tk_col[1:]) - 1
        except ValueError:
            return None
        if idx < 0 or idx >= len(self._cols):
            return None
        return self._cols[idx]

    def _is_filter_arrow_click(self, x: int, tk_col: str) -> bool:
        col = self._column_key_from_tk(tk_col)
        if not col:
            return False
        try:
            idx = int(tk_col[1:]) - 1
        except ValueError:
            return False
        left = 0
        for i in range(idx):
            left += int(self.tree.column(self._cols[i], "width"))
        col_width = int(self.tree.column(col, "width"))
        base = self._headings[col]
        if col in self._active_filters:
            base = f"{base} [筛]"
        label = f"{base}{self._filter_arrow}"
        label_width = int(self._heading_font.measure(label))
        left_padding = max(0, (col_width - label_width) // 2)
        arrow_prefix = int(self._heading_font.measure(f"{base} "))
        arrow_width = max(8, int(self._heading_font.measure("▼")))
        arrow_left = left + left_padding + arrow_prefix
        arrow_right = arrow_left + arrow_width + 2
        return arrow_left <= x <= arrow_right

    def _update_heading_labels(self) -> None:
        for col in self._cols:
            base = self._headings[col]
            if col in self._active_filters:
                base = f"{base} [筛]"
            self.tree.heading(col, text=f"{base}{self._filter_arrow}", anchor=tk.CENTER)

    def _passes_filters(self, s: "ClientSession") -> bool:
        if not self._active_filters:
            return True
        values = dict(zip(self._cols, self._session_values(s)))
        for col, allowed in self._active_filters.items():
            if col not in values:
                continue
            if str(values[col]) not in allowed:
                return False
        return True

    def _rebuild_tree(self) -> None:
        prev_selection = [str(iid) for iid in self.tree.selection()]
        prev_focus = str(self.tree.focus()) if self.tree.focus() else ""
        yview = self.tree.yview()
        top_frac = float(yview[0]) if yview else 0.0

        self.tree.delete(*self.tree.get_children())
        inserted_ids: List[str] = []
        for s in self._sessions_cache:
            if not self._passes_filters(s):
                continue
            self.tree.insert("", tk.END, iid=s.session_id, values=self._session_values(s))
            inserted_ids.append(str(s.session_id))

        if inserted_ids:
            self.tree.yview_moveto(top_frac)

        keep_selection = [iid for iid in prev_selection if iid in inserted_ids]
        if keep_selection:
            self.tree.selection_set(keep_selection)
            if prev_focus and prev_focus in keep_selection:
                self.tree.focus(prev_focus)
            else:
                self.tree.focus(keep_selection[-1])

    def _open_column_filter(self, tk_col: str, x_root: Optional[int] = None, y_root: Optional[int] = None) -> None:
        col = self._column_key_from_tk(tk_col)
        if not col:
            return
        values = sorted({str(dict(zip(self._cols, self._session_values(s)))[col] or "") for s in self._sessions_cache})
        if not values:
            messagebox.showinfo("筛选", "当前暂无数据可筛选。")
            return

        d = tk.Toplevel(self)
        d.title(f"筛选：{self._headings[col]}")
        d.transient(self)
        d.grab_set()
        if x_root is not None and y_root is not None:
            d.geometry(f"+{x_root - 8}+{y_root + 8}")

        lst = tk.Listbox(d, selectmode=tk.MULTIPLE, width=36, height=12)
        lst.grid(row=0, column=0, columnspan=3, padx=8, pady=(8, 6), sticky="nsew")
        for item in values:
            lst.insert(tk.END, item if item else "(空)")

        current = self._active_filters.get(col)
        if current:
            for i, v in enumerate(values):
                if v in current:
                    lst.select_set(i)

        def apply_filter() -> None:
            selected = {values[i] for i in lst.curselection()}
            if selected:
                self._active_filters[col] = selected
            else:
                self._active_filters.pop(col, None)
            self._update_heading_labels()
            self._rebuild_tree()
            d.destroy()

        def clear_filter() -> None:
            self._active_filters.pop(col, None)
            self._update_heading_labels()
            self._rebuild_tree()
            d.destroy()

        ttk.Button(d, text="应用筛选", command=apply_filter).grid(row=1, column=0, padx=8, pady=(0, 8), sticky="w")
        ttk.Button(d, text="清除此列", command=clear_filter).grid(row=1, column=1, padx=4, pady=(0, 8))
        ttk.Button(d, text="取消", command=d.destroy).grid(row=1, column=2, padx=8, pady=(0, 8), sticky="e")
        d.rowconfigure(0, weight=1)
        d.columnconfigure(0, weight=1)
        d.columnconfigure(2, weight=1)

    def refresh_sessions(self, sessions: List["ClientSession"]) -> None:
        self._sessions_cache = list(sessions)
        self._update_heading_labels()
        self._rebuild_tree()

    def _dlg_rename(self) -> None:
        sids = self.selected_session_ids()
        if not sids:
            messagebox.showinfo("提示", "请先选择一台学生机。")
            return
        if len(sids) > 1:
            messagebox.showinfo("提示", "修改计算机名只能选择一台学生机。")
            return
        sid = sids[0]
        d = tk.Toplevel(self)
        d.title("修改计算机名")
        d.transient(self)
        d.grab_set()
        ttk.Label(d, text="新计算机名:").grid(row=0, column=0, padx=8, pady=8, sticky=tk.W)
        ent = ttk.Entry(d, width=36)
        ent.grid(row=1, column=0, padx=8, pady=(0, 8), sticky=tk.EW)

        def ok() -> None:
            name = ent.get().strip()
            if not name:
                messagebox.showwarning("校验", "名称不能为空。", parent=d)
                return
            cmd_id = self.next_cmd_id()
            if self._on_enqueue_rename(sid, cmd_id, name):
                self.log_line(
                    "已排队: 改名 pc_name=%s cmd_id=%s session=%s -> %s"
                    % (self._pc_name_for_session(sid), cmd_id, sid, name)
                )
                d.destroy()
            else:
                messagebox.showerror("错误", "会话不存在或已断开。", parent=d)

        bf = ttk.Frame(d)
        bf.grid(row=2, column=0, pady=8)
        ttk.Button(bf, text="确定", command=ok).pack(side=tk.LEFT, padx=4)
        ttk.Button(bf, text="取消", command=d.destroy).pack(side=tk.LEFT, padx=4)
        d.columnconfigure(0, weight=1)

    def _dlg_ipv4(self) -> None:
        sids = self.selected_session_ids()
        if not sids:
            messagebox.showinfo("提示", "请先选择一台学生机。")
            return
        if len(sids) > 1:
            messagebox.showinfo("提示", "修改网卡IPv4地址只能选择一台学生机。")
            return
        sid = sids[0]
        d = tk.Toplevel(self)
        d.title("修改网卡 IPv4")
        d.transient(self)
        d.grab_set()
        mode = tk.StringVar(value="static")
        ttk.Radiobutton(d, text="静态 IPv4", variable=mode, value="static").grid(
            row=0, column=0, sticky=tk.W, padx=8, pady=(8, 2)
        )
        ttk.Radiobutton(d, text="DHCP 自动获取", variable=mode, value="dhcp").grid(
            row=1, column=0, sticky=tk.W, padx=8, pady=2
        )

        f = ttk.LabelFrame(d, text="静态参数", padding=6)
        f.grid(row=2, column=0, sticky="ew", padx=8, pady=6)
        labels = ["IP 地址", "子网掩码（点分或前缀如 24）", "默认网关（可空）", "主 DNS（可空）", "辅 DNS（可空）"]
        entries: List[ttk.Entry] = []
        for i, lab in enumerate(labels):
            ttk.Label(f, text=lab + ":").grid(row=i, column=0, sticky=tk.W, pady=2)
            e = ttk.Entry(f, width=32)
            e.grid(row=i, column=1, sticky=tk.EW, padx=(8, 0), pady=2)
            entries.append(e)
        f.columnconfigure(1, weight=1)

        hint = ttk.Label(
            d,
            text="将作用于「默认网关所在网卡」。改 IP 可能导致 Agent 短暂断线后自动重连。",
            wraplength=420,
        )
        hint.grid(row=3, column=0, padx=8, pady=4, sticky=tk.W)

        def ok() -> None:
            m = mode.get()
            payload: Dict[str, Any] = {"mode": m}
            if m == "static":
                ip = entries[0].get().strip()
                mask = entries[1].get().strip()
                gw = entries[2].get().strip()
                dns1 = entries[3].get().strip()
                dns2 = entries[4].get().strip()
                if not ip or not mask:
                    messagebox.showwarning("校验", "静态模式需要 IP 与子网掩码。", parent=d)
                    return
                payload["ip"] = ip
                payload["mask"] = mask
                if gw:
                    payload["gateway"] = gw
                if dns1:
                    payload["dns_primary"] = dns1
                if dns2:
                    payload["dns_secondary"] = dns2
            cmd_id = self.next_cmd_id()
            if self._on_enqueue_set_ipv4(sid, cmd_id, payload):
                self.log_line(
                    "已排队: 设 IP pc_name=%s cmd_id=%s session=%s %s"
                    % (
                        self._pc_name_for_session(sid),
                        cmd_id,
                        sid,
                        json.dumps(payload, ensure_ascii=False),
                    )
                )
                d.destroy()
            else:
                messagebox.showerror("错误", "会话不存在或已断开。", parent=d)

        bf = ttk.Frame(d)
        bf.grid(row=4, column=0, pady=8)
        ttk.Button(bf, text="确定", command=ok).pack(side=tk.LEFT, padx=4)
        ttk.Button(bf, text="取消", command=d.destroy).pack(side=tk.LEFT, padx=4)
        d.columnconfigure(0, weight=1)

    def _dlg_power(self) -> None:
        sids = self.selected_session_ids()
        if not sids:
            messagebox.showinfo("提示", "请先选择学生机。")
            return
        d = tk.Toplevel(self)
        d.title("电源操作")
        d.transient(self)
        d.grab_set()
        def run_power_action(act: str) -> None:
            for sid in sids:
                cmd_id = self.next_cmd_id()
                if self._on_enqueue_power(sid, cmd_id, act):
                    self.log_line(
                        "已排队: 电源 pc_name=%s cmd_id=%s session=%s action=%s"
                        % (self._pc_name_for_session(sid), cmd_id, sid, act)
                    )
                else:
                    self.log_line(
                        "失败: 电源操作 pc_name=%s session=%s 会话不存在或已断开"
                        % (self._pc_name_for_session(sid), sid)
                    )
            d.destroy()

        bf = ttk.Frame(d, padding=8)
        bf.grid(row=0, column=0)
        ttk.Button(bf, text="关机", command=lambda: run_power_action("shutdown")).pack(
            side=tk.LEFT, padx=(0, 6)
        )
        ttk.Button(bf, text="重启", command=lambda: run_power_action("reboot")).pack(
            side=tk.LEFT
        )

    def _dlg_network_restrict(self) -> None:
        sids = self.selected_session_ids()
        if not sids:
            messagebox.showinfo("提示", "请先选择学生机。")
            return
        d = tk.Toplevel(self)
        d.title("网络访问限制")
        d.transient(self)
        d.grab_set()
        d.geometry("550x520")

        config = _load_config()
        saved_mode = config.get("network_restrict", {}).get("mode", "blacklist")
        saved_rules = config.get("network_restrict", {}).get("rules", [])

        mode = tk.StringVar(value=saved_mode)
        modes_frame = ttk.LabelFrame(d, text="限制模式", padding=6)
        modes_frame.grid(row=0, column=0, columnspan=2, sticky="ew", padx=8, pady=(8, 0))
        
        ttk.Radiobutton(modes_frame, text="黑名单模式（禁止访问指定目标）", variable=mode, value="blacklist").grid(
            row=0, column=0, sticky=tk.W, padx=4, pady=2
        )
        ttk.Radiobutton(modes_frame, text="白名单模式（仅允许访问指定目标）", variable=mode, value="whitelist").grid(
            row=1, column=0, sticky=tk.W, padx=4, pady=2
        )
        ttk.Radiobutton(modes_frame, text="完全断网（阻止所有出站连接）", variable=mode, value="block_all").grid(
            row=2, column=0, sticky=tk.W, padx=4, pady=2
        )
        ttk.Radiobutton(modes_frame, text="解除限制（恢复正常网络）", variable=mode, value="disable").grid(
            row=3, column=0, sticky=tk.W, padx=4, pady=2
        )

        rules_frame = ttk.LabelFrame(d, text="规则列表（黑名单/白名单模式）", padding=6)
        rules_frame.grid(row=1, column=0, columnspan=2, sticky="nsew", padx=8, pady=6)
        mihomo_types = [
            "DOMAIN",
            "DOMAIN-SUFFIX",
            "DOMAIN-KEYWORD",
            "DOMAIN-WILDCARD",
            "DOMAIN-REGEX",
            "GEOSITE",
            "IP-CIDR",
            "IP-CIDR6",
            "IP-SUFFIX",
            "IP-ASN",
            "GEOIP",
            "SRC-GEOIP",
            "SRC-IP-ASN",
            "SRC-IP-CIDR",
            "SRC-IP-SUFFIX",
            "DST-PORT",
            "SRC-PORT",
            "IN-PORT",
            "IN-TYPE",
            "IN-USER",
            "IN-NAME",
            "PROCESS-PATH",
            "PROCESS-PATH-WILDCARD",
            "PROCESS-PATH-REGEX",
            "PROCESS-NAME",
            "PROCESS-NAME-WILDCARD",
            "PROCESS-NAME-REGEX",
            "UID",
            "NETWORK",
            "DSCP",
            "RULE-SET",
            "AND",
            "OR",
            "NOT",
            "SUB-RULE",
            "MATCH",
        ]
        type_zh_map: Dict[str, str] = {
            "DOMAIN": "完整域名",
            "DOMAIN-SUFFIX": "域名后缀",
            "DOMAIN-KEYWORD": "域名关键字",
            "DOMAIN-WILDCARD": "域名通配符",
            "DOMAIN-REGEX": "域名正则",
            "GEOSITE": "Geosite",
            "IP-CIDR": "目标 IP 段",
            "IP-CIDR6": "目标 IPv6 段",
            "IP-SUFFIX": "目标 IP 后缀",
            "IP-ASN": "目标 IP ASN",
            "GEOIP": "目标 IP 国家",
            "SRC-GEOIP": "来源 IP 国家",
            "SRC-IP-ASN": "来源 IP ASN",
            "SRC-IP-CIDR": "来源 IP 段",
            "SRC-IP-SUFFIX": "来源 IP 后缀",
            "DST-PORT": "目标端口",
            "SRC-PORT": "来源端口",
            "IN-PORT": "入站端口",
            "IN-TYPE": "入站类型",
            "IN-USER": "入站用户",
            "IN-NAME": "入站名称",
            "PROCESS-PATH": "进程路径",
            "PROCESS-PATH-WILDCARD": "进程路径通配符",
            "PROCESS-PATH-REGEX": "进程路径正则",
            "PROCESS-NAME": "进程名",
            "PROCESS-NAME-WILDCARD": "进程名通配符",
            "PROCESS-NAME-REGEX": "进程名正则",
            "UID": "用户 ID",
            "NETWORK": "网络协议",
            "DSCP": "DSCP 标记",
            "RULE-SET": "规则集",
            "AND": "逻辑与",
            "OR": "逻辑或",
            "NOT": "逻辑非",
            "SUB-RULE": "子规则",
            "MATCH": "兜底匹配",
        }
        type_desc_map: Dict[str, str] = {
            "DOMAIN": "匹配完整域名。",
            "DOMAIN-SUFFIX": "匹配域名后缀，如 google.com 可匹配 www.google.com。",
            "DOMAIN-KEYWORD": "匹配域名关键字。",
            "DOMAIN-WILDCARD": "域名通配符匹配，仅支持 * 和 ?（与 Clash 规则里其它通配符写法不同）。",
            "DOMAIN-REGEX": "域名正则表达式匹配。",
            "GEOSITE": "匹配 Geosite 内域名分组。",
            "IP-CIDR": "匹配目标 IP 地址范围（CIDR）。",
            "IP-CIDR6": "匹配目标 IPv6 地址范围（CIDR，IP-CIDR 别名）。",
            "IP-SUFFIX": "匹配目标 IP 后缀范围。",
            "IP-ASN": "匹配目标 IP 所属 ASN。",
            "GEOIP": "匹配目标 IP 所属国家代码。",
            "SRC-GEOIP": "匹配来源 IP 所属国家代码。",
            "SRC-IP-ASN": "匹配来源 IP 所属 ASN。",
            "SRC-IP-CIDR": "匹配来源 IP 地址范围（CIDR）。",
            "SRC-IP-SUFFIX": "匹配来源 IP 后缀范围。",
            "DST-PORT": "匹配请求目标端口（支持端口范围）。",
            "SRC-PORT": "匹配请求来源端口（支持端口范围）。",
            "IN-PORT": "匹配入站端口（支持端口范围）。",
            "IN-TYPE": "匹配入站类型。",
            "IN-USER": "匹配入站用户名，可用 / 分隔多个用户名。",
            "IN-NAME": "匹配入站名称。",
            "PROCESS-PATH": "使用完整进程路径匹配。",
            "PROCESS-PATH-WILDCARD": "进程路径通配符匹配，仅支持 * 和 ?。",
            "PROCESS-PATH-REGEX": "进程路径正则表达式匹配。",
            "PROCESS-NAME": "使用进程名匹配（Android 可匹配包名）。",
            "PROCESS-NAME-WILDCARD": "进程名通配符匹配，仅支持 * 和 ?（Android 可匹配包名）。",
            "PROCESS-NAME-REGEX": "进程名正则表达式匹配（Android 可匹配包名）。",
            "UID": "匹配 Linux 用户 ID。",
            "NETWORK": "匹配网络协议 tcp 或 udp。",
            "DSCP": "匹配 DSCP 标记（仅 tproxy udp 入站）。",
            "RULE-SET": "引用规则集合（需配置 rule-providers）。",
            "AND": "逻辑与：AND,((payload1),(payload2)),Policy，注意括号。",
            "OR": "逻辑或：OR,((payload1),(payload2)),Policy，注意括号。",
            "NOT": "逻辑非：NOT,((payload)),Policy，注意括号。",
            "SUB-RULE": "子规则匹配：SUB-RULE,(payload),Policy，注意括号。",
            "MATCH": "匹配所有请求，无需条件（通常放最后）。",
        }
        type_display_values = [f"{type_zh_map.get(t, t)} ({t})" for t in mihomo_types]
        display_to_type = {f"{type_zh_map.get(t, t)} ({t})": t for t in mihomo_types}
        rules_data: List[Dict[str, str]] = []

        rule_input = ttk.Frame(rules_frame)
        rule_input.grid(row=0, column=0, columnspan=2, sticky="ew")
        ttk.Label(rule_input, text="类型:").grid(row=0, column=0, sticky="w", pady=2)
        type_var = tk.StringVar(value=next((x for x in type_display_values if x.endswith("(DOMAIN-SUFFIX)")), type_display_values[0]))
        type_combo = ttk.Combobox(
            rule_input,
            textvariable=type_var,
            values=type_display_values,
            width=20,
            state="readonly",
        )
        type_combo.grid(row=0, column=1, sticky="w", padx=(6, 10), pady=2)

        ttk.Label(rule_input, text="匹配值:").grid(row=0, column=2, sticky="w", pady=2)
        payload_entry = ttk.Entry(rule_input, width=26)
        payload_entry.grid(row=0, column=3, sticky="ew", padx=(6, 10), pady=2)

        type_desc_var = tk.StringVar(value=type_desc_map.get("DOMAIN-SUFFIX", ""))
        type_desc_label = ttk.Label(
            rule_input,
            textvariable=type_desc_var,
            foreground="gray",
            wraplength=520,
        )
        type_desc_label.grid(row=2, column=0, columnspan=5, sticky="w", pady=(2, 0))

        ttk.Label(rule_input, text="附加参数(可空):").grid(row=1, column=0, sticky="w", pady=(4, 2))
        extra_entry = ttk.Entry(rule_input, width=30)
        extra_entry.grid(row=1, column=1, columnspan=3, sticky="ew", padx=(6, 10), pady=(4, 2))
        ttk.Button(rule_input, text="添加", width=12, command=lambda: _add_rule()).grid(
            row=1, column=4, sticky="e", padx=(4, 0), pady=(4, 2), ipady=2
        )
        rule_input.columnconfigure(3, weight=1)
        rule_input.columnconfigure(4, weight=1)

        def _sync_type_desc(*_args: object) -> None:
            selected_type = type_var.get().strip()
            rule_type = display_to_type.get(selected_type, selected_type.upper())
            type_desc_var.set(type_desc_map.get(rule_type, ""))

        type_var.trace_add("write", _sync_type_desc)
        _sync_type_desc()

        rules_list = tk.Listbox(rules_frame, height=8, selectmode=tk.MULTIPLE)
        rules_list.grid(row=1, column=0, columnspan=2, sticky="nsew", pady=4)

        btn_frame = ttk.Frame(rules_frame)
        btn_frame.grid(row=2, column=0, columnspan=2, sticky="ew")
        ttk.Button(btn_frame, text="全选", command=lambda: _select_all()).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(btn_frame, text="删除选中", command=lambda: _remove_rule()).pack(side=tk.LEFT)
        ttk.Button(btn_frame, text="上移", command=lambda: _move_rule(-1)).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Button(btn_frame, text="下移", command=lambda: _move_rule(1)).pack(side=tk.LEFT, padx=(6, 0))

        def _legacy_to_mihomo(rule: Dict[str, str]) -> Optional[Dict[str, str]]:
            rule_type = str(rule.get("type", "")).strip().lower()
            value = str(rule.get("value", "")).strip()
            if not rule_type or not value:
                return None
            if rule_type == "domain":
                return {"type": "DOMAIN-SUFFIX", "payload": value, "policy": "", "extra": ""}
            if rule_type == "ip":
                return {"type": "IP-CIDR", "payload": f"{value}/32", "policy": "", "extra": ""}
            if rule_type == "subnet":
                return {"type": "IP-CIDR", "payload": value, "policy": "", "extra": ""}
            return None

        def _format_rule_text(rule: Dict[str, str]) -> str:
            rt = str(rule.get("type", "")).strip().upper()
            payload = str(rule.get("payload", "")).strip()
            extra = str(rule.get("extra", "")).strip()
            text = f"{type_zh_map.get(rt, rt)} ({rt})"
            if payload:
                text += f", {payload}"
            if extra:
                text += f", {extra}"
            return text

        def _refresh_rule_list() -> None:
            rules_list.delete(0, tk.END)
            for r in rules_data:
                rules_list.insert(tk.END, _format_rule_text(r))

        for rule in saved_rules:
            if not isinstance(rule, dict):
                continue
            rule_type = str(rule.get("type", "")).strip().upper()
            payload = str(rule.get("payload", "")).strip()
            if payload or rule_type == "MATCH":
                rules_data.append(
                    {
                        "type": rule_type or "DOMAIN-SUFFIX",
                        "payload": payload,
                        "policy": str(rule.get("policy", "")).strip().upper(),
                        "extra": str(rule.get("extra", "")).strip(),
                    }
                )
                continue
            legacy = _legacy_to_mihomo(rule)
            if legacy:
                rules_data.append(legacy)
        _refresh_rule_list()

        rules_frame.rowconfigure(1, weight=1)
        rules_frame.columnconfigure(0, weight=1)

        count_text = f"已选择 {len(sids)} 台学生机" if len(sids) > 1 else "已选择 1 台学生机"
        count_label = ttk.Label(
            d,
            text=count_text,
            foreground="gray"
        )
        count_label.grid(row=2, column=0, columnspan=2, padx=8, pady=(0, 2), sticky="w")
        
        hint = ttk.Label(
            d,
            text=(
                "注意：规则按顺序匹配。MATCH 无需匹配值；"
                "AND/OR/NOT/SUB-RULE 的匹配值请按 mihomo 语法填写括号；"
                "附加参数仅支持 no-resolve/src（仅目标 IP 规则可用）。"
            ),
            wraplength=520,
            foreground="gray"
        )
        hint.grid(row=3, column=0, columnspan=2, padx=8, pady=4, sticky="w")

        def _add_rule():
            selected_type = type_var.get().strip()
            rule_type = display_to_type.get(selected_type, selected_type.upper())
            payload = payload_entry.get().strip()
            extra = extra_entry.get().strip().lower()

            no_payload_types = {"MATCH"}
            if rule_type not in no_payload_types and not payload:
                messagebox.showwarning("提示", "该规则类型需要填写匹配值。", parent=d)
                return
            if rule_type in no_payload_types:
                payload = ""

            # no-resolve / src 仅对目标 IP 规则生效
            if extra:
                extra_items = [x.strip() for x in extra.split(",") if x.strip()]
                allowed_extras = {"no-resolve", "src"}
                if not extra_items or any(x not in allowed_extras for x in extra_items):
                    messagebox.showwarning("提示", "附加参数仅支持 no-resolve 或 src。", parent=d)
                    return
                target_ip_types = {"IP-CIDR", "IP-CIDR6", "IP-SUFFIX", "IP-ASN", "GEOIP"}
                if rule_type not in target_ip_types:
                    messagebox.showwarning(
                        "提示",
                        "附加参数 no-resolve/src 仅可用于目标 IP 规则（IP-CIDR/IP-CIDR6/IP-SUFFIX/IP-ASN/GEOIP）。",
                        parent=d,
                    )
                    return
                # 规整格式，避免空格和重复项
                deduped: List[str] = []
                for item in extra_items:
                    if item not in deduped:
                        deduped.append(item)
                extra = ",".join(deduped)
            rules_data.append(
                {"type": rule_type, "payload": payload, "policy": "", "extra": extra}
            )
            _refresh_rule_list()
            payload_entry.delete(0, tk.END)
            extra_entry.delete(0, tk.END)

        def _select_all():
            rules_list.select_set(0, tk.END)

        def _remove_rule():
            sel = rules_list.curselection()
            for i in reversed(sel):
                del rules_data[i]
            _refresh_rule_list()

        def _move_rule(direction: int) -> None:
            sel = list(rules_list.curselection())
            if not sel:
                return
            if direction < 0:
                if sel[0] <= 0:
                    return
                for idx in sel:
                    rules_data[idx - 1], rules_data[idx] = rules_data[idx], rules_data[idx - 1]
                new_sel = [idx - 1 for idx in sel]
            else:
                if sel[-1] >= len(rules_data) - 1:
                    return
                for idx in reversed(sel):
                    rules_data[idx + 1], rules_data[idx] = rules_data[idx], rules_data[idx + 1]
                new_sel = [idx + 1 for idx in sel]
            _refresh_rule_list()
            for idx in new_sel:
                rules_list.select_set(idx)

        def ok():
            selected_mode = mode.get()
            rules = []
            for r in rules_data:
                rule_type = str(r.get("type", "")).strip().upper()
                payload = str(r.get("payload", "")).strip()
                if not rule_type:
                    continue
                if rule_type != "MATCH" and not payload:
                    continue
                rules.append(
                    {
                        "type": rule_type,
                        "payload": payload,
                        "policy": "",
                        "extra": str(r.get("extra", "")).strip(),
                    }
                )
            
            config = _load_config()
            config["network_restrict"] = {
                "mode": selected_mode,
                "rules": rules
            }
            _save_config(config)

            clash_config = build_clash_config(selected_mode, rules)
            yaml_content = config_to_yaml(clash_config)
            
            update_local_config = len(sids) >= 2
            if update_local_config:
                yaml_dir = os.path.dirname(_get_config_path())
                os.makedirs(yaml_dir, exist_ok=True)
                yaml_path = os.path.join(yaml_dir, "config.yaml")
                if selected_mode == "disable":
                    if os.path.exists(yaml_path):
                        os.remove(yaml_path)
                        self.log_line(f"已删除本地配置文件: {yaml_path}")
                    else:
                        self.log_line(f"本地配置文件不存在，跳过删除: {yaml_path}")
                else:
                    with open(yaml_path, "w", encoding="utf-8") as f:
                        f.write(yaml_content)
                    self.log_line(f"已更新本地配置文件: {yaml_path}")
            else:
                self.log_line("选择1台学生机，跳过本地配置文件更新")

            payload: Dict[str, Any] = {"mode": selected_mode, "config": yaml_content}
            
            if selected_mode in ("blacklist", "whitelist"):
                if selected_mode == "whitelist" and not rules:
                    messagebox.showwarning("校验", "白名单模式需要至少添加一个允许的目标。", parent=d)
                    return
                payload["rules"] = rules
            
            mode_name = {
                "blacklist": "黑名单",
                "whitelist": "白名单",
                "block_all": "断网",
                "disable": "解除限制"
            }.get(selected_mode, selected_mode)
            
            for sid in sids:
                cmd_id = self.next_cmd_id()
                if self._on_enqueue_network_restrict(sid, cmd_id, payload):
                    self.log_line(
                        "已排队: 网络限制 pc_name=%s cmd_id=%s session=%s mode=%s"
                        % (self._pc_name_for_session(sid), cmd_id, sid, mode_name)
                    )
                else:
                    self.log_line(
                        "失败: 网络限制 pc_name=%s session=%s 会话不存在或已断开"
                        % (self._pc_name_for_session(sid), sid)
                    )
            d.destroy()

        bf = ttk.Frame(d)
        bf.grid(row=4, column=0, columnspan=2, pady=8)
        ttk.Button(bf, text="确定", command=ok).pack(side=tk.RIGHT, padx=4)
        ttk.Button(bf, text="取消", command=d.destroy).pack(side=tk.RIGHT, padx=4)

        d.rowconfigure(1, weight=1)
        d.rowconfigure(4, weight=0)
        d.columnconfigure(0, weight=1)
