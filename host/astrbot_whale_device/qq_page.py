"""The complete QQ login flow lives in one desktop page."""
from __future__ import annotations

import tkinter as tk
import customtkinter as ctk

from .desktop_components import BG, WHITE, INK, MUTED, GREEN, SOFT, LINE, FONT, button, entry, label, textbox


def build_qq_page(w, page):
    w.qq_page = page
    page.grid_columnconfigure(0, weight=1)
    page.grid_rowconfigure(1, weight=1)
    status = ctk.CTkFrame(page, fg_color=WHITE, corner_radius=14)
    status.grid(row=0, column=0, sticky='ew', pady=(0, 12))
    status.grid_columnconfigure(1, weight=1)
    w.qq_status_label = ctk.CTkLabel(status, text='未连接 QQ', height=30, corner_radius=15,
        fg_color=SOFT, text_color=MUTED, font=(FONT, 12))
    w.qq_status_label.grid(row=0, column=0, padx=(16, 12), pady=(14, 4), sticky='w')
    w.qq_account_label = label(status, '尚未读取助手账号', 13, True, wraplength=270)
    w.qq_account_label.grid(row=0, column=1, pady=(14, 4), sticky='w')
    w.qq_auto_reply_var = tk.BooleanVar(value=False)
    w.qq_auto_login_var = tk.BooleanVar(value=False)
    w.qq_auto_reply_switch = ctk.CTkSwitch(status, text='自动回复', variable=w.qq_auto_reply_var,
        command=w._on_qq_auto_reply, font=(FONT, 12), progress_color=GREEN,
        button_color=WHITE, button_hover_color='#EAF3ED', fg_color='#CEDBD2', text_color=INK,
        switch_width=36, switch_height=20, width=100)
    w.qq_auto_reply_switch.grid(row=0, column=2, padx=16, pady=(14, 4), sticky='e')
    w.qq_auto_login_switch = ctk.CTkSwitch(status, text='启动 EXE 时自动登录', variable=w.qq_auto_login_var, command=w._on_qq_auto_login, font=(FONT, 12), progress_color=GREEN, button_color=WHITE, fg_color='#CEDBD2', text_color=INK, switch_width=36, switch_height=20, width=150)
    w.qq_auto_login_switch.grid(row=0, column=3, padx=(0, 16), pady=(14, 4), sticky='e')
    w.qq_detail_label = label(status, '准备登录服务，扫码确认后自动接通消息。', 11,
        color=MUTED, wraplength=420, justify='left', height=0)
    w.qq_detail_label.grid(row=1, column=0, columnspan=2, sticky='w', padx=16, pady=(3, 12))
    w.qq_activity_label = label(status, '尚未收到 QQ 消息', 10, color=MUTED, height=0)
    w.qq_activity_label.grid(row=1, column=2, sticky='e', padx=16, pady=(3, 12))

    w.qq_tabs = ctk.CTkTabview(page, height=350, fg_color=WHITE, corner_radius=14,
        segmented_button_fg_color=SOFT, segmented_button_selected_color='#CBE8D5',
        segmented_button_selected_hover_color='#B4DCC3', segmented_button_unselected_color=SOFT,
        segmented_button_unselected_hover_color='#DCEEE3', text_color=INK,
        text_color_disabled=MUTED, border_width=0)
    w.qq_tabs.grid(row=1, column=0, sticky='nsew')
    for name in ('登录助手', '运行日志', '高级设置'):
        w.qq_tabs.add(name)
    login = w.qq_tabs.tab('登录助手')
    login.grid_columnconfigure(0, weight=1)
    login.grid_rowconfigure(1, weight=1)
    actions = ctk.CTkFrame(login, fg_color='transparent')
    actions.grid(row=0, column=0, sticky='ew', padx=10, pady=(8, 10))
    w.qq_login_start_btn = button(actions, '准备并显示登录码', w._on_qq_manual_login, True, width=156)
    w.qq_login_start_btn.pack(side='left', padx=(0, 8))
    w.qq_qr_refresh_btn = button(actions, '刷新二维码', w._on_qq_qr_refresh, width=110, state='disabled')
    w.qq_qr_refresh_btn.pack(side='left', padx=(0, 8))
    w.qq_stop_login_btn = button(actions, '停止登录服务', w._on_qq_login_stop, width=118, state='disabled')
    w.qq_stop_login_btn.pack(side='left', padx=(0, 8))
    w.qq_logout_btn = button(actions, '下线助手 QQ', w._on_qq_logout, width=108)
    w.qq_logout_btn.pack(side='left')

    body = ctk.CTkFrame(login, fg_color='transparent', corner_radius=0)
    body.grid(row=1, column=0, sticky='nsew', padx=10, pady=(0, 8))
    body.grid_columnconfigure(1, weight=1)
    body.grid_rowconfigure(0, weight=1)
    qr_box = ctk.CTkFrame(body, fg_color='#F7FAF8', corner_radius=12, border_width=1, border_color=LINE)
    qr_box.grid(row=0, column=0, sticky='n', padx=(0, 20))
    w.qq_qr_size = round(168 * w.root._get_window_scaling())
    w.qq_qr_canvas = tk.Canvas(qr_box, width=w.qq_qr_size, height=w.qq_qr_size, background=WHITE,
        highlightthickness=0, borderwidth=0)
    w.qq_qr_canvas.pack(padx=12, pady=(12, 8))
    w.qq_qr_canvas.create_text(w.qq_qr_size // 2, w.qq_qr_size // 2, text='登录二维码将显示在这里', width=w.qq_qr_size - 24,
        fill=MUTED, font=(FONT, 11), justify='center', tags='placeholder')
    w.qq_qr_caption = label(qr_box, '等待准备登录服务', 10, color=MUTED, height=18)
    w.qq_qr_caption.pack(pady=(0, 12))
    details = ctk.CTkFrame(body, fg_color='transparent')
    details.grid(row=0, column=1, sticky='nsew')
    label(details, '在这里登录助手 QQ', 16, True, height=22).pack(anchor='w', pady=(4, 8))
    w.qq_login_hint = label(details, '首次使用会自动准备登录组件。出现二维码后，用手机 QQ 扫码并确认登录。',
        11, color=MUTED, wraplength=280, justify='left', height=0)
    w.qq_login_hint.pack(fill='x', pady=(0, 10))
    w.qq_login_steps = []
    for text in ('准备电脑登录服务', '手机扫码并确认登录', '自动接通消息通道'):
        step = label(details, '○  ' + text, 11, color=MUTED, height=18)
        step.pack(anchor='w', pady=2)
        w.qq_login_steps.append(step)
    w.qq_login_progress = ctk.CTkProgressBar(details, height=5, progress_color=GREEN, fg_color=SOFT)
    w.qq_login_progress.set(0)
    w.qq_login_progress.pack(fill='x', pady=(12, 6))
    w.qq_login_progress_label = label(details, '尚未开始', 10, color=MUTED, wraplength=280, justify='left', height=0)
    w.qq_login_progress_label.pack(fill='x')

    logs = w.qq_tabs.tab('运行日志')
    logs.grid_rowconfigure(1, weight=1)
    logs.grid_columnconfigure(0, weight=1)
    log_actions = ctk.CTkFrame(logs, fg_color='transparent')
    log_actions.grid(row=0, column=0, sticky='ew', padx=8, pady=(8, 10))
    label(log_actions, '登录服务与消息处理记录', 13, True).pack(side='left')
    button(log_actions, '清空显示', w._on_qq_clear_logs, width=100).pack(side='right')
    w.qq_log_text = textbox(logs, height=200, wrap='word')
    w.qq_log_text.grid(row=1, column=0, sticky='nsew', padx=8, pady=(0, 8))
    w.qq_log_text.configure(state='disabled')

    advanced = ctk.CTkScrollableFrame(w.qq_tabs.tab('高级设置'), fg_color='transparent',
        scrollbar_button_color='#D0DED5')
    advanced.pack(fill='both', expand=True)
    label(advanced, '已有 NapCat 时，也可以手动接入', 15, True).pack(anchor='w', pady=(8, 4))
    label(advanced, '通常无需填写。自动登录流程会准备连接并保存设置。', 11, color=MUTED).pack(anchor='w', pady=(0, 10))
    w.qq_url_var = tk.StringVar(value='ws://127.0.0.1:3001')
    w.qq_token_var = tk.StringVar()
    w.qq_webui_url_var = tk.StringVar(value='http://127.0.0.1:6099')
    w.qq_webui_token_var = tk.StringVar()
    w.qq_totp_var = tk.StringVar()
    w.qq_launcher_var = tk.StringVar()
    w.qq_qq_path_var = tk.StringVar()
    def field(title, variable, secret=False):
        label(advanced, title, 12).pack(anchor='w', pady=(8, 5))
        control = entry(advanced, variable, secret=secret)
        control.pack(fill='x', pady=(0, 5))
        return control
    w.qq_url_input = field('消息连接地址', w.qq_url_var)
    w.qq_token_input = field('消息连接 Token', w.qq_token_var, True)
    w.qq_webui_url_input = field('本机登录服务地址', w.qq_webui_url_var)
    w.qq_webui_token_input = field('已有服务的管理 Token（自动准备时无需填写）', w.qq_webui_token_var, True)
    field('动态验证码（已有服务启用两步验证时填写）', w.qq_totp_var, True)
    field('已有 NapCat 启动文件（可选）', w.qq_launcher_var)
    button(advanced, '选择已有启动文件', w._on_qq_pick_launcher, width=150).pack(anchor='w', pady=(6, 12))
    field('QQ 安装位置（自动识别失败时选择 QQ.exe）', w.qq_qq_path_var)
    button(advanced, '选择 QQ.exe', w._on_qq_pick_qq, width=150).pack(anchor='w', pady=(6, 12))
    manual = ctk.CTkFrame(advanced, fg_color='transparent')
    manual.pack(fill='x', pady=(4, 12))
    w.qq_connect_btn = button(manual, '连接已有消息服务', w._on_qq_connect, True, width=150)
    w.qq_connect_btn.pack(side='left', padx=(0, 8))
    w.qq_disconnect_btn = button(manual, '断开消息', w._on_qq_disconnect, width=106, state='disabled')
    w.qq_disconnect_btn.pack(side='left', padx=(0, 8))
    button(manual, '保存设置', w._on_qq_save_settings, width=106).pack(side='left')

    footer = ctk.CTkFrame(page, fg_color='transparent')
    footer.grid(row=2, column=0, sticky='ew', pady=(8, 0))
    w.qq_recent_log = label(footer, '登录、连接和服务日志都在本页的“运行日志”中。', 10, color=MUTED,
        wraplength=590, justify='left', height=0)
    w.qq_recent_log.pack(fill='x')
    w.qq_runtime_label = label(footer, '关闭窗口后留在托盘运行，退出托盘才会停止服务。',
        10, color=MUTED, wraplength=590, justify='left', height=0)
    w.qq_runtime_label.pack(fill='x', pady=(3, 0))
    w.qq_tabs.set('登录助手')
