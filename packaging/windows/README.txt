ectype, portable build for Windows
==================================

Nothing to install. Unzip the folder anywhere you like (Desktop, Documents, a USB stick) and
run one of the two files in it.

    ectype-gui.bat      double-click this one. It opens the web UI in your browser.
    ectype.bat          the command line, for a Command Prompt opened in this folder.

A black console window belongs to the web UI while it is running. Leave it open. Closing it,
or pressing Ctrl+C in it, stops the program.

Windows may warn you before the first run, because the launchers are unsigned scripts from
the internet. On the blue "Windows protected your PC" screen, Run anyway appears only after
you click More info. Unblocking the zip before you unzip it (right-click it, Properties, tick
Unblock, OK) avoids the warning.


What is in this folder
----------------------

    python\             Python itself, the official embeddable build from python.org.
                        It is used only by this folder and is not installed on your system:
                        nothing is added to PATH, the registry, or your Python installation
                        if you have one.
    ectype\             the program.
    user\               created the first time you change a setting. It holds settings.json.


Removing it
-----------

Delete the folder. That is all of it. The one thing kept outside is anything you deliberately
exported or backed up, which goes where you chose to put it.


Where your sessions are read from
---------------------------------

ectype reads the session files the agents already write, in place, and does not modify them.
Run "ectype.bat agents" to see which stores were found on this machine and the exact path used
for each. If one is in an unusual place, set it in the web UI under Settings.


Is this the same program as "pip install ectype-cli"?
-----------------------------------------------------

Yes, the same code and the same version. This build exists so that it can be run without a
Python installation or a terminal. If you already use Python, "pipx install ectype-cli" is the
tidier route and updates itself.


Licence
-------

GPL-3.0-or-later. Source, issues and the full README:

    https://github.com/chaybits/ectype
