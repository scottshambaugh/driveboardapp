# -*- coding: UTF-8 -*-
import os
import sys
import itertools
import webbrowser

import queue
import tkinter as tk

from config import conf

__author__  = 'Stefan Hechenberger <stefan@nortd.com>'


def init():
    root = tk.Tk()
    root.title("DriveboardApp Server")
    img = tk.PhotoImage(file=os.path.join(conf['rootdir'], 'backend', 'icon.gif'))
    root.tk.call('wm', 'iconphoto', root._w, img)
    root.geometry('1200x400')
    # root.iconify()  # not working as expected on osx


    # text widget
    text = tk.Text(root, wrap=tk.NONE)
    text.pack(expand=True, side=tk.LEFT, fill=tk.BOTH) 
    scrolly = tk.Scrollbar(text, command=text.yview, orient=tk.VERTICAL)
    scrolly.pack(side=tk.RIGHT, fill=tk.Y)
    scrollx = tk.Scrollbar(text, command=text.xview, orient=tk.HORIZONTAL)
    scrollx.pack(side=tk.BOTTOM, fill=tk.X)
    text.config(yscrollcommand = scrolly.set, xscrollcommand = scrollx.set)
    scrolly.set(0.0,1.0)
    
    # add copy to clipboard feature
    def copy(event):
        selected = text.get("sel.first", "sel.last")
        if selected:
            root.clipboard_clear()
            root.clipboard_append(selected)
    text.bind("<Control-c>", copy)


    # open frontend button
    def open_browser():
        print('Opening browser interface...')
        try:
            webbrowser.open_new_tab('http://127.0.0.1:4444')
        except webbrowser.Error:
            print("Cannot open Webbrowser, please do so manually. Address: http://127.0.0.1:4444")
    tk.Button(text, text="Open Browser Interface", command=open_browser).pack(side=tk.BOTTOM)


    # output queue, required because of tkinter thread issues
    q = queue.Queue()

    class OutputHandler(object):
        def write(self, msg):
            q.put(msg)
        def flush(self):
            pass

    stdout_old = sys.stdout
    stderr_old = sys.stderr
    output = OutputHandler()
    sys.stdout = output
    sys.stderr = output


    # output consumer, a recursive tkinter callback
    update_callback_id = None
    def iterex(function, exception):
        # helper func, like builtin iter() but stops on exception
        try:
            while True:
                yield function()
        except exception:
            return

    def update(q):
        if scrolly.get()[1] == 1.0:
            for line in itertools.islice(iterex(q.get_nowait, queue.Empty), 10000):
                if line is None:
                    return  # stop updating
                else:
                    text.insert(tk.END, line)
                    text.see(tk.END)
                    root.focus()
        global update_callback_id
        update_callback_id = root.after(40, update, q)  # schedule next update
    update(q)  # start recursive updates


    # good exiting behavior
    def quit():
        global update_callback_id
        root.after_cancel(update_callback_id)
        sys.stdout = stdout_old
        sys.stderr = stderr_old
        root.destroy()
    root.protocol("WM_DELETE_WINDOW", quit)
    root.bind("<Control-q>", lambda event: quit())


    return root


if __name__ == "__main__":
    root = init()
    root.mainloop()
