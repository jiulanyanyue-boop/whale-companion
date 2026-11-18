"""Whale desktop: every interactive surface uses the shared CTk component system."""
from __future__ import annotations
import tkinter as tk
import customtkinter as ctk
from .desktop_components import *
from .app_picker import AppPicker


def build(w):
    root=w.root
    root.geometry('1180x780')
    root.minsize(1060,680)
    root.configure(fg_color=BG)
    root.grid_columnconfigure(1,weight=1)
    root.grid_rowconfigure(0,weight=1)
    sidebar=ctk.CTkFrame(root,width=206,corner_radius=0,fg_color=WHITE)
    sidebar.grid(row=0,column=0,sticky='nsew')
    sidebar.grid_propagate(False)
    brand=ctk.CTkFrame(sidebar,fg_color='transparent')
    brand.pack(fill='x',padx=24,pady=(32,36))
    ctk.CTkLabel(brand,text='W',width=42,height=42,corner_radius=14,fg_color=GREEN,
        text_color=WHITE,font=('Segoe UI',24,'bold')).pack(side='left',padx=(0,10))
    label(brand,'Whale',20,True).pack(anchor='w')
    label(brand,'手机助手',11,color=MUTED).pack(anchor='w')
    label(sidebar,'我的工作空间',11,color=MUTED).pack(anchor='w',padx=26,pady=(0,12))
    main=ctk.CTkFrame(root,fg_color='transparent',corner_radius=0)
    main.grid(row=0,column=1,sticky='nsew',padx=30,pady=(28,22))
    main.grid_columnconfigure(0,weight=1)
    main.grid_rowconfigure(2,weight=1)
    header=ctk.CTkFrame(main,fg_color='transparent')
    header.grid(row=0,column=0,sticky='ew',pady=(0,7))
    w.page_title=tk.StringVar(value='总览')
    label(header,textvariable=w.page_title,size=27,bold=True).pack(side='left')
    w.device_badge=tk.StringVar(value='主机未启动')
    ctk.CTkLabel(header,textvariable=w.device_badge,height=32,corner_radius=16,
        fg_color=SOFT,text_color=GREEN,font=(FONT,12)).pack(side='right')
    subtitle=tk.StringVar()
    label(main,textvariable=subtitle,size=13,color=MUTED).grid(row=1,column=0,sticky='w',pady=(0,22))
    stage=ctk.CTkFrame(main,fg_color='transparent',corner_radius=0)
    stage.grid(row=2,column=0,sticky='nsew')
    stage.grid_rowconfigure(0,weight=1);stage.grid_columnconfigure(0,weight=1)
    pages={};nav={}
    descriptions={'总览':'连接你的手机，掌控自己的设备。','手机配对':'扫描电脑上的二维码，并在手机上核对指纹。',
        'QQ接入':'在这里扫码登录助手 QQ，自动接通消息并查看运行日志。',
        '模型设置':'连接你使用的模型服务，为助手准备好思考能力。',
        '应用权限':'只允许操作你选中的应用，修改后记得保存。','运行日志':'查看连接与操作记录，快速了解运行情况。'}
    def navigate(name):
        for key,p in pages.items():
            if key==name:p.grid(row=0,column=0,sticky='nsew')
            else:p.grid_remove()
        w.page_title.set(name);subtitle.set(descriptions[name])
        if hasattr(pages[name], '_parent_canvas'):
            root.after(100, lambda p=pages[name]: p._parent_canvas.yview_moveto(0))
        for key,b in nav.items():
            b.configure(fg_color=SOFT if key==name else 'transparent',text_color=GREEN if key==name else MUTED)
    w.navigate=navigate
    for name in descriptions:
        b=ctk.CTkButton(sidebar,text=name,command=lambda n=name:navigate(n),height=46,
            corner_radius=10,anchor='w',font=(FONT,14),fg_color='transparent',
            hover_color=SOFT,text_color=MUTED)
        b.pack(fill='x',padx=14,pady=4);nav[name]=b
        if name=='应用权限':page=ctk.CTkFrame(stage,fg_color='transparent',corner_radius=0)
        else:page=ctk.CTkScrollableFrame(stage,fg_color='transparent',corner_radius=0,
            scrollbar_button_color='#D0DED5',scrollbar_button_hover_color='#A0BBAA')
        pages[name]=page
    label(sidebar,'你的设备，由你掌控',11,color=MUTED).pack(side='bottom',anchor='w',padx=24,pady=26)

    def card(parent,title,note=''):
        box=ctk.CTkFrame(parent,fg_color=WHITE,corner_radius=18)
        box.pack(fill='x',pady=(0,16),padx=(0,5))
        content=ctk.CTkFrame(box,fg_color='transparent')
        content.pack(fill='both',expand=True,padx=24,pady=22)
        label(content,title,17,True).pack(anchor='w',pady=(0,6))
        if note:label(content,note,12,color=MUTED,wraplength=760,justify='left').pack(anchor='w',pady=(0,18))
        return content
    def field(parent,title,var,secret=False,placeholder=''):
        label(parent,title,13).pack(anchor='w',pady=(8,7))
        e=entry(parent,var,placeholder,secret);e.pack(fill='x',pady=(0,12));return e
    def save_config():
        if w._on_save_quiet():
            w.toast.configure(text='配置已保存',text_color=GREEN)
        else:w.toast.configure(text=getattr(w,'_last_save_error','') or '未能保存，请检查配置',text_color='#AB4242')
    w.toast=label(main,'',12,color=GREEN)
    w.toast.grid(row=3,column=0,sticky='w',pady=(8,0))

    home=pages['总览']
    hero=ctk.CTkFrame(home,fg_color=GREEN,corner_radius=20)
    hero.pack(fill='x',pady=(0,18),padx=(0,5))
    label(hero,'让手机成为你的得力助手',25,True,color=WHITE).pack(anchor='w',padx=28,pady=(26,10))
    label(hero,'从一次安全连接开始，按自己的习惯选择模型与应用。',13,color='#D8EDDF').pack(anchor='w',padx=28,pady=(0,22))
    button(hero,'连接我的手机',lambda:navigate('手机配对'),width=152).pack(anchor='w',padx=28,pady=(0,26))
    control=card(home,'主机服务','启动主机后，手机就可以连接这台电脑。')
    controls=ctk.CTkFrame(control,fg_color='transparent');controls.pack(fill='x')
    w.astrbot_control=controls
    w.start_btn=button(controls,'启动主机',w._on_start,True,width=130);w.start_btn.pack(side='left',padx=(0,10))
    w.stop_btn=button(controls,'停止主机',w._on_stop,state='disabled',width=120);w.stop_btn.pack(side='left')
    owner=card(home,'远程控制账号','仅允许该 QQ 账号操作手机，其他账号不会获得权限。')
    w.owner_var=tk.StringVar();field(owner,'你的 QQ 号',w.owner_var,placeholder='填写自己的 QQ 号')
    button(owner,'保存账号',save_config,True).pack(anchor='w',pady=(6,0))
    qq_entry=card(home,'QQ 消息助手','助手 QQ 由电脑上的 NapCat 登录。你的 QQ 号只用于识别主人，不会在这里登录。')
    button(qq_entry,'设置 QQ 接入',lambda:navigate('QQ接入')).pack(anchor='w')

    pair=card(pages['手机配对'],'与手机建立连接','在手机助手中打开扫一扫；配对完成前请核对两端指纹。')
    pairbody=ctk.CTkFrame(pair,fg_color='transparent');pairbody.pack(fill='x',pady=10)
    tile=ctk.CTkFrame(pairbody,fg_color='#F1F7F3',corner_radius=16)
    tile.pack(side='left',anchor='n',padx=(0,24))
    # The QR canvas is a drawing surface, not an interactive legacy widget.
    w.qr_canvas=tk.Canvas(tile,width=262,height=262,bg='#F1F7F3',highlightthickness=0)
    w.qr_canvas.pack(padx=12,pady=12)
    meta=ctk.CTkFrame(pairbody,fg_color='transparent');meta.pack(side='left',fill='both',expand=True)
    w.pair_code_var=tk.StringVar(value='—');w.pair_url_var=tk.StringVar(value='—')
    w.pair_ttl_var=tk.StringVar(value='—');w.pair_fingerprint_var=tk.StringVar(value='—')
    for title,var in [('配对码',w.pair_code_var),('有效时间',w.pair_ttl_var),('连接地址',w.pair_url_var),('主机指纹',w.pair_fingerprint_var)]:
        label(meta,title,11,color=MUTED).pack(anchor='w',pady=(6,3))
        label(meta,textvariable=var,size=13,wraplength=320,justify='left').pack(anchor='w')
    w.qr_fallback=label(pair,'',12,color=MUTED,wraplength=740,justify='left');w.qr_fallback.pack(fill='x',pady=12)
    button(pair,'刷新配对码',w._on_regen).pack(anchor='w')

    model=card(pages['模型设置'],'模型连接','填写服务商提供的地址和 API Key。模型可以从列表选择，也可以手动输入。')
    w.model_base_var=tk.StringVar();w.model_key_var=tk.StringVar();w.model_id_var=tk.StringVar();w.signing_secret_var=tk.StringVar()
    field(model,'服务地址',w.model_base_var,placeholder='https://api.example.com/v1')
    keyrow=ctk.CTkFrame(model,fg_color='transparent');keyrow.pack(fill='x')
    keyrow.grid_columnconfigure(0,weight=1)
    label(keyrow,'API Key',13).grid(row=0,column=0,sticky='w',pady=(8,7))
    keyinput=entry(keyrow,w.model_key_var,secret=True);keyinput.grid(row=1,column=0,sticky='ew',pady=(0,12))
    shown=tk.BooleanVar(value=False)
    ctk.CTkSwitch(keyrow,text='显示',variable=shown,command=lambda:keyinput.configure(show='' if shown.get() else '•'),
        font=(FONT,12),progress_color=GREEN,button_color=WHITE,button_hover_color='#EAF3ED',
        fg_color='#CEDBD2',text_color=MUTED).grid(row=1,column=1,padx=(14,0),pady=(0,12))
    button(model,'获取可用模型',w._on_fetch_models).pack(anchor='w',pady=(2,16))
    label(model,'使用的模型',13).pack(anchor='w',pady=(0,7))
    w.model_id_combo=ctk.CTkComboBox(model,variable=w.model_id_var,values=[],height=44,corner_radius=10,
        border_width=1,border_color=LINE,fg_color='#F8FAF9',button_color=SOFT,button_hover_color='#DCEEE3',
        dropdown_fg_color=WHITE,dropdown_hover_color=SOFT,dropdown_text_color=INK,text_color=INK,
        font=(FONT,14),dropdown_font=(FONT,13));w.model_id_combo.pack(fill='x',pady=(0,12))
    w.vision_hint=label(model,'识图提醒：QQ 收到的图片会作为图片内容发给这里选择的模型，请选择服务商标明支持图片输入（视觉/识图）的模型。不支持识图的模型可能报错或看不到图片。图片以链接形式发送时，模型服务商需要能访问该链接。',12,color=MUTED,wraplength=740);w.vision_hint.pack(anchor='w',pady=(0,8))
    w.model_hint=label(model,'',12,color=MUTED,wraplength=740);w.model_hint.pack(anchor='w')
    w.secrets_status=label(model,'密钥保存在本机',12,color=MUTED,wraplength=740);w.secrets_status.pack(anchor='w',pady=12)
    button(model,'保存模型配置',save_config,True).pack(anchor='w',pady=(4,0))

    from .qq_page import build_qq_page
    build_qq_page(w, pages['QQ接入'])

    apppage=pages['应用权限']
    apppage.grid_columnconfigure(0,weight=1);apppage.grid_rowconfigure(1,weight=1)
    apphead=ctk.CTkFrame(apppage,fg_color='transparent');apphead.grid(row=0,column=0,sticky='ew',pady=(0,14))
    w.app_count_label=label(apphead,'连接手机后获取真实应用列表',12,color=MUTED)
    w.app_count_label.pack(side='left')
    w.fetch_apps_btn=button(apphead,'刷新手机应用',w._on_fetch_apps,width=128,state='disabled');w.fetch_apps_btn.pack(side='right')
    w.app_picker=AppPicker(apppage,save=lambda:w._on_save_quiet())
    w.app_picker.grid(row=1,column=0,sticky='nsew')

    logs=card(pages['运行日志'],'运行记录','连接和操作结果会记录在这里。')
    w.log_text=textbox(logs,height=370,wrap='word');w.log_text.pack(fill='both',expand=True)
    w.log_text.configure(state='disabled')
    logactions=ctk.CTkFrame(logs,fg_color='transparent');logactions.pack(fill='x',pady=(16,0))
    button(logactions,'连接配置',w._on_advanced).pack(side='left',padx=(0,10))
    button(logactions,'密钥状态',w._on_check_env).pack(side='left')
    advanced=card(pages['运行日志'],'高级应用配置','仅在手机未列出所需应用时使用包名补充。')
    w.manual_packages_visible=tk.BooleanVar(value=False)
    w.allowed_text=textbox(advanced,height=80)
    def toggle():
        if w.manual_packages_visible.get():w.allowed_text.pack(fill='x',pady=(12,0))
        else:w.allowed_text.pack_forget()
    ctk.CTkSwitch(advanced,text='手动填写包名',variable=w.manual_packages_visible,command=toggle,
        font=(FONT,13),progress_color=GREEN,button_color=WHITE,fg_color='#CFDCD4',text_color=INK).pack(anchor='w')
    button(advanced,'保存高级配置',save_config).pack(anchor='w',pady=(14,0))
    navigate('应用权限')
    w._log('界面已就绪。连接手机后，可搜索并勾选允许控制的应用。')
