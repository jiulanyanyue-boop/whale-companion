"""Searchable application cards with stable checkbox selection and a selected tray."""
from __future__ import annotations
import tkinter as tk
import customtkinter as ctk
from .app_selection import AppSelection
from .desktop_components import *


class AppPicker(ctk.CTkFrame):
    def __init__(self, master, save):
        super().__init__(master, fg_color='transparent', corner_radius=0)
        self.state = AppSelection()
        self.query = tk.StringVar()
        self.mode = tk.StringVar(value='全部应用')
        self._timer = None
        self._cards = {}
        self._save = save
        self.grid_columnconfigure(0, weight=1)
        self.grid_columnconfigure(1, weight=0, minsize=232)
        self.grid_rowconfigure(2, weight=1)
        bar = ctk.CTkFrame(self, fg_color=WHITE, corner_radius=16)
        bar.grid(row=0, column=0, columnspan=2, sticky='ew', pady=(0,12))
        bar.grid_columnconfigure(0, weight=1)
        self.search = entry(bar, self.query, '搜索应用名称或包名')
        label(bar,'搜索应用',12,color=MUTED).grid(row=0,column=0,sticky='w',padx=16,pady=(12,2))
        self.search.grid(row=1, column=0, sticky='ew', padx=(16,8), pady=(0,14))
        button(bar, '清空搜索', lambda:self.query.set(''), width=88).grid(row=1,column=1,padx=(0,16),pady=(0,14))
        filters = ctk.CTkFrame(self, fg_color='transparent')
        filters.grid(row=1,column=0,sticky='ew',pady=(0,12),padx=(0,16))
        self.filters=ctk.CTkSegmentedButton(filters, values=['全部应用','已选择','未选择'],variable=self.mode,
            command=lambda _:self.render(),height=34,corner_radius=8,fg_color='#E5EDE8',
            selected_color=GREEN,selected_hover_color=HOVER,unselected_color='#E5EDE8',
            unselected_hover_color='#DCE9E0',text_color=INK,font=(FONT,12))
        self.filters.pack(side='left')
        self.result_label=label(filters,'',12,color=MUTED)
        self.result_label.pack(side='right')
        self.scroller=ctk.CTkScrollableFrame(self,fg_color='transparent',corner_radius=0,
            scrollbar_button_color='#CFDDD4',scrollbar_button_hover_color='#A3BDAD')
        self.scroller.grid(row=2,column=0,sticky='nsew',padx=(0,16))
        self.scroller.grid_columnconfigure((0,1),weight=1,uniform='app')
        self.tray=ctk.CTkFrame(self,fg_color=WHITE,corner_radius=16,width=232)
        self.tray.grid(row=1,column=1,rowspan=2,sticky='nsew')
        self.tray.grid_propagate(False)
        self.tray.grid_columnconfigure(0,weight=1)
        self.tray.grid_rowconfigure(2,weight=1)
        self.count_label=label(self.tray,'已选择 0 个',17,True)
        self.count_label.grid(row=0,column=0,sticky='w',padx=18,pady=(20,4))
        label(self.tray,'仅这些应用允许被操作',12,color=MUTED).grid(row=1,column=0,sticky='w',padx=18,pady=(0,16))
        self.selected_list=ctk.CTkScrollableFrame(self.tray,fg_color='transparent',corner_radius=0,width=200)
        self.selected_list.grid(row=2,column=0,sticky='nsew',padx=10)
        self.selected_list.grid_columnconfigure(0,weight=1)
        self.clear_button=button(self.tray,'清空已选',self.clear_selection)
        self.clear_button.grid(row=3,column=0,sticky='ew',padx=18,pady=16)
        footer=ctk.CTkFrame(self,fg_color=WHITE,corner_radius=14)
        footer.grid(row=3,column=0,columnspan=2,sticky='ew',pady=(16,0))
        footer.grid_columnconfigure(0,weight=1)
        self.save_status=label(footer,'尚未修改',13,color=MUTED,wraplength=320,justify='left')
        self.save_status.grid(row=0,column=0,sticky='w',padx=18,pady=16)
        self.bulk=button(footer,'选择当前结果',self.select_visible,width=120)
        self.bulk.grid(row=0,column=1,padx=(0,10),pady=12)
        button(footer,'保存应用权限',self.save,primary=True,width=136).grid(row=0,column=2,padx=(0,16),pady=12)
        self.query.trace_add('write',self._search_changed)
        self.render()

    def _search_changed(self,*_):
        if self._timer is not None:self.after_cancel(self._timer)
        self._timer=self.after(150,self._searched)

    def _searched(self):
        self._timer=None
        self.render()

    def set_apps(self,apps):
        self.state.replace_apps(apps)
        self.render()

    def load(self,packages):
        self.state.load(packages)
        self.render()

    def packages(self):
        return self.state.packages()

    def toggle(self,package,enabled):
        self.state.toggle(package,enabled)
        if self.mode.get()!='全部应用':
            self.render()
        else:
            self._sync_card(package)
            self._summary()

    def select_visible(self):
        for app in self.state.visible(self.query.get(),self.mode.get()):self.state.toggle(app.package_name,True)
        self.render()

    def clear_selection(self):
        self.state.selected.clear()
        self.render()

    def save(self):
        if self._save():
            self.state.saved=set(self.state.selected)
            self._summary('应用权限已保存')
        else:self.save_status.configure(text='未能保存，请先完成账号和模型配置',text_color='#AB4242')

    def _sync_card(self,package):
        if package not in self._cards:return
        frame,var,_=self._cards[package]
        chosen=package in self.state.selected
        var.set(chosen)
        frame.configure(fg_color='#EDF7F0' if chosen else WHITE,
            border_color=GREEN if chosen else LINE)

    def _summary(self,message=None):
        count=len(self.state.selected)
        self.count_label.configure(text=f'已选择 {count} 个')
        self.clear_button.configure(state='normal' if count else 'disabled')
        self.save_status.configure(text=message or ('有未保存的更改' if self.state.dirty else '与已保存配置一致'),
            text_color=GREEN if message else '#AC7B29' if self.state.dirty else MUTED)
        for child in self.selected_list.winfo_children():child.destroy()
        from collections import Counter
        counts=Counter(a.label for a in self.state.apps)
        names={a.package_name:(f'{a.label}\n{a.package_name}' if counts[a.label]>1 else a.label) for a in self.state.apps}
        if not count:
            label(self.selected_list,'还没有选择应用\n勾选左侧应用即可添加',12,color=MUTED,justify='left',wraplength=170).grid(row=0,column=0,sticky='w',padx=8,pady=12)
        for i,package in enumerate(sorted(self.state.selected,key=lambda p:names.get(p,p).casefold())):
            chip=ctk.CTkFrame(self.selected_list,fg_color=SOFT,corner_radius=10)
            chip.grid(row=i,column=0,sticky='ew',pady=4,padx=3)
            chip.grid_columnconfigure(0,weight=1)
            label(chip,names.get(package,package),12,wraplength=125).grid(row=0,column=0,sticky='w',padx=(10,2),pady=10)
            button(chip,'×',lambda p=package:self.toggle(p,False),width=26).grid(row=0,column=1,padx=(0,6),pady=4)

    def render(self):
        position=self.scroller._parent_canvas.yview()[0] if self._cards else 0.0
        for child in self.scroller.winfo_children():child.destroy()
        self._cards={}
        visible=self.state.visible(self.query.get(),self.mode.get())
        self.result_label.configure(text=f'{len(visible)} / {len(self.state.apps)} 个应用')
        self.bulk.configure(state='normal' if visible else 'disabled')
        if not visible:
            empty=ctk.CTkFrame(self.scroller,fg_color=WHITE,corner_radius=16)
            empty.grid(row=0,column=0,columnspan=2,sticky='ew',padx=2,pady=4)
            label(empty,'没有匹配的应用' if self.state.apps else '先连接手机，获取应用列表',18,True).pack(padx=28,pady=(48,12))
            label(empty,'试试其他关键词或切换筛选。' if self.state.apps else '获取后，在这里按应用名称勾选。',13,color=MUTED).pack(padx=28,pady=(0,48))
        for i,app in enumerate(visible):
            frame=ctk.CTkFrame(self.scroller,fg_color=WHITE,corner_radius=14,border_width=1,border_color=LINE)
            frame.grid(row=i//2,column=i%2,sticky='ew',padx=4,pady=5)
            frame.grid_columnconfigure(1,weight=1)
            # A name tile, not a fabricated app icon.
            tile=ctk.CTkLabel(frame,text=app.label[:1].upper(),width=42,height=42,
                corner_radius=12,fg_color=SOFT,text_color=GREEN,font=(FONT,18,'bold'))
            tile.grid(row=0,column=0,rowspan=2,padx=(14,10),pady=16)
            title=label(frame,app.label,14,True,wraplength=140)
            title.grid(row=0,column=1,sticky='w',pady=(14,1))
            detail=label(frame,app.package_name,10,color=MUTED,wraplength=148)
            detail.grid(row=1,column=1,sticky='w',pady=(0,14))
            var=tk.BooleanVar(value=app.package_name in self.state.selected)
            check=ctk.CTkCheckBox(frame,text='',variable=var,width=26,checkbox_width=22,checkbox_height=22,
                corner_radius=6,border_width=2,border_color='#BCCEC2',fg_color=GREEN,
                hover_color='#B8DCC5',checkmark_color=WHITE,
                command=lambda p=app.package_name,v=var:self.toggle(p,v.get()))
            check.grid(row=0,column=2,rowspan=2,padx=(4,14))
            for view in (frame,tile,title,detail):
                view.bind('<Button-1>',lambda e,p=app.package_name:self.toggle(p,p not in self.state.selected))
            self._cards[app.package_name]=(frame,var,check)
            self._sync_card(app.package_name)
        self._summary()
        self.scroller.after_idle(lambda:self.scroller._parent_canvas.yview_moveto(position))
