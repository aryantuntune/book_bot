"""Tkinter-based UI for the bundled .exe. Two entry points:

- prompt_startup(): one-shot dialog at launch collecting operator phone,
  input xlsx path, and the debug/keep-open toggles. Returns a dict or None
  if the user cancels.
- prompt_otp(phone): modal popup shown mid-run when HPCL SMSes the OTP to
  the operator. Returns the entered string or '' on cancel.

tkinter is Python stdlib and ships with PyInstaller automatically, so no
new runtime dependency. If tkinter fails to import (e.g. a headless Linux
box without Tk), callers should fall back to CLI prompts.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional, TypedDict


class StartupValues(TypedDict):
    operator_phone: str
    input_file: Path
    debug: bool
    keep_open: bool


def prompt_startup() -> Optional[StartupValues]:
    import tkinter as tk
    from tkinter import filedialog, messagebox

    result: dict = {"submitted": False}

    root = tk.Tk()
    root.title("HP Gas Booking Bot")
    root.resizable(False, False)
    try:
        root.iconbitmap(default="")
    except tk.TclError:
        pass

    frm = tk.Frame(root, padx=16, pady=14)
    frm.grid()

    tk.Label(
        frm,
        text="HP Gas Booking Bot",
        font=("Segoe UI", 13, "bold"),
    ).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 2))
    tk.Label(
        frm,
        text="Fill in the details below and click Start.",
        font=("Segoe UI", 9),
        fg="#555",
    ).grid(row=1, column=0, columnspan=3, sticky="w", pady=(0, 12))

    tk.Label(frm, text="Operator phone:", font=("Segoe UI", 10)).grid(
        row=2, column=0, sticky="e", padx=(0, 8), pady=4,
    )
    phone_var = tk.StringVar()
    phone_entry = tk.Entry(frm, textvariable=phone_var, width=32, font=("Segoe UI", 10))
    phone_entry.grid(row=2, column=1, columnspan=2, sticky="we", pady=4)
    tk.Label(
        frm,
        text="(10-digit number registered with HP Gas for OTP)",
        font=("Segoe UI", 8),
        fg="#777",
    ).grid(row=3, column=1, columnspan=2, sticky="w")

    tk.Label(frm, text="Input Excel file:", font=("Segoe UI", 10)).grid(
        row=4, column=0, sticky="e", padx=(0, 8), pady=(12, 4),
    )
    file_var = tk.StringVar()
    file_entry = tk.Entry(frm, textvariable=file_var, width=32, font=("Segoe UI", 10))
    file_entry.grid(row=4, column=1, sticky="we", pady=(12, 4))

    def _browse() -> None:
        path = filedialog.askopenfilename(
            title="Select input .xlsx file",
            filetypes=[("Excel files", "*.xlsx"), ("All files", "*.*")],
        )
        if path:
            file_var.set(path)

    tk.Button(frm, text="Browse...", command=_browse, width=10).grid(
        row=4, column=2, sticky="w", padx=(6, 0), pady=(12, 4),
    )
    tk.Label(
        frm,
        text="(Column A=consumer no, Column B=phone; no header)",
        font=("Segoe UI", 8),
        fg="#777",
    ).grid(row=5, column=1, columnspan=2, sticky="w")

    debug_var = tk.BooleanVar(value=False)
    keep_open_var = tk.BooleanVar(value=False)
    tk.Checkbutton(
        frm, text="Debug logging", variable=debug_var, font=("Segoe UI", 9),
    ).grid(row=6, column=1, sticky="w", pady=(12, 0))
    tk.Checkbutton(
        frm,
        text="Keep browser open on error (for debugging)",
        variable=keep_open_var,
        font=("Segoe UI", 9),
    ).grid(row=7, column=1, columnspan=2, sticky="w")

    btn_frame = tk.Frame(frm)
    btn_frame.grid(row=8, column=0, columnspan=3, sticky="e", pady=(16, 0))

    def _validate_and_submit() -> None:
        phone = phone_var.get().strip()
        path_str = file_var.get().strip()
        digits = "".join(ch for ch in phone if ch.isdigit())
        if len(digits) != 10:
            messagebox.showerror(
                "Invalid phone",
                "Operator phone must be exactly 10 digits.",
                parent=root,
            )
            return
        if not path_str:
            messagebox.showerror(
                "Missing file", "Please select an input .xlsx file.", parent=root,
            )
            return
        p = Path(path_str)
        if not p.exists():
            messagebox.showerror(
                "File not found", f"File does not exist:\n{p}", parent=root,
            )
            return
        result["submitted"] = True
        result["operator_phone"] = digits
        result["input_file"] = p
        result["debug"] = bool(debug_var.get())
        result["keep_open"] = bool(keep_open_var.get())
        root.destroy()

    def _cancel() -> None:
        root.destroy()

    tk.Button(
        btn_frame, text="Cancel", command=_cancel, width=10,
    ).grid(row=0, column=0, padx=(0, 6))
    start_btn = tk.Button(
        btn_frame, text="Start", command=_validate_and_submit, width=10,
        default="active",
    )
    start_btn.grid(row=0, column=1)

    root.bind("<Return>", lambda _e: _validate_and_submit())
    root.bind("<Escape>", lambda _e: _cancel())
    phone_entry.focus_set()

    root.update_idletasks()
    w = root.winfo_reqwidth()
    h = root.winfo_reqheight()
    sw = root.winfo_screenwidth()
    sh = root.winfo_screenheight()
    root.geometry(f"+{(sw - w) // 2}+{(sh - h) // 2}")

    root.mainloop()

    if not result.get("submitted"):
        return None
    return {
        "operator_phone": result["operator_phone"],
        "input_file": result["input_file"],
        "debug": result["debug"],
        "keep_open": result["keep_open"],
    }


def prompt_otp(operator_phone: str) -> str:
    import tkinter as tk
    from tkinter import simpledialog

    root = tk.Tk()
    root.withdraw()
    try:
        value = simpledialog.askstring(
            "Enter OTP",
            f"Enter the OTP sent to {operator_phone}:",
            show="*",
            parent=root,
        )
    finally:
        root.destroy()
    return (value or "").strip()
