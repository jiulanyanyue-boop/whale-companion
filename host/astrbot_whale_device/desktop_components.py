"""Shared CustomTkinter components used across the desktop interface."""
from __future__ import annotations
import tkinter as tk
import customtkinter as ctk

BG = '#F3F7F5'
WHITE = '#FFFFFF'
INK = '#203B30'
MUTED = '#7B8D83'
GREEN = '#248653'
HOVER = '#1C7045'
SOFT = '#E8F4ED'
LINE = '#E1EAE5'
FONT = 'Microsoft YaHei UI'


def label(parent, text='', size=14, bold=False, color=INK, **kwargs):
    return ctk.CTkLabel(parent, text=text, font=(FONT, size, 'bold' if bold else 'normal'),
                        text_color=color, anchor='w', **kwargs)


def button(parent, text, command, primary=False, **kwargs):
    return ctk.CTkButton(parent, text=text, command=command, height=40, corner_radius=10,
        font=(FONT, 13), fg_color=GREEN if primary else SOFT,
        hover_color=HOVER if primary else '#DCEEE3', text_color='white' if primary else GREEN,
        text_color_disabled='#A2B2A8', border_width=0, **kwargs)


def entry(parent, variable=None, placeholder='', secret=False, **kwargs):
    return ctk.CTkEntry(parent, textvariable=variable, placeholder_text=placeholder,
        show='•' if secret else '', height=44, corner_radius=10, border_width=1,
        fg_color='#F8FAF9', border_color=LINE, text_color=INK,
        placeholder_text_color=MUTED, font=(FONT, 14), **kwargs)


def textbox(parent, height=100, **kwargs):
    return ctk.CTkTextbox(parent, height=height, corner_radius=12, border_width=1,
        border_color=LINE, fg_color='#F8FAF9', text_color=INK, font=(FONT, 13),
        scrollbar_button_color='#CEDCD3', scrollbar_button_hover_color='#9CB7A6', **kwargs)


def notice(root, title, message, error=False):
    """Non-blocking application-styled dialog; a single close action."""
    dialog = ctk.CTkToplevel(root)
    dialog.title(title)
    dialog.geometry('500x330')
    dialog.minsize(440, 300)
    dialog.configure(fg_color=BG)
    dialog.transient(root)
    card = ctk.CTkFrame(dialog, fg_color=WHITE, corner_radius=18)
    card.pack(fill='both', expand=True, padx=20, pady=20)
    label(card, title, 19, True, '#AB4242' if error else INK).pack(fill='x', padx=22, pady=(20,10))
    text = textbox(card, height=145, wrap='word')
    text.pack(fill='both', expand=True, padx=22)
    text.insert('1.0', message)
    text.configure(state='disabled')
    button(card, '知道了', dialog.destroy, primary=True).pack(anchor='e', padx=22, pady=16)
    dialog.after(100, lambda: dialog.grab_set() if dialog.winfo_exists() else None)
    return dialog
