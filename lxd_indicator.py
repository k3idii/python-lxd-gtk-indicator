
import signal
import os
import argparse
import time
import threading
import json
import yaml

import pylxd
import gi
gi.require_version('Gtk', '3.0')
gi.require_version('AppIndicator3', '0.1')
from gi.repository import Gtk
from gi.repository import Gdk
from gi.repository import GLib
from gi.repository import AppIndicator3
from gi.repository import GObject

gi.require_version("Notify", "0.7")
from gi.repository import Notify


APPNAME = "LXD-indicator"
VERSION = "1.0"

ROOTDIR = os.path.dirname(__file__)
APPDIR = os.path.realpath(ROOTDIR)

LXD_ICON = os.path.join(APPDIR, 'icons/container.svg')
ICO_UP   = os.path.join(APPDIR, 'icons/up.svg')
ICO_DOWN = os.path.join(APPDIR, 'icons/down.svg')


STR_RUNNING = "Running"
STR_STOPPED = "Stopped"

LXD_SHELL_COMMAND = "lxc shell {name}"
TERMINAL_COMMAND="xfce4-terminal -e '{cmd}'"


## EVENTS : https://github.com/lxc/lxd/blob/master/doc/events.md

LXD_EVENT_LC_START     = 'instance-started'
LXD_EVENT_LC_SHUTDOWN  = 'instance-shutdown'
LXD_EVENT_LC_STOP      = 'instance-stopped'

LXD_EVENTS_UP   =  [ LXD_EVENT_LC_START]
LXD_EVENTS_DOWN =  [ LXD_EVENT_LC_SHUTDOWN, LXD_EVENT_LC_STOP]


class MyWebSocket(pylxd.client._WebsocketClient):
  callback = None
  interesting_events = ['lifecycle']
  # ^--- can be empty list or/and [ 'logging', 'operation' ]
  #  \-- can be changed during runtime

  def received_message(self, message):
    msg = json.loads(str(message)) # do it anyway
    if len(self.interesting_events) > 0:
      if msg['type'] not in self.interesting_events:
        return
    # this way we can handle even in external code w/o need to re-define whole class
    if self.callback is not None:
      self.callback(msg)
    else:
      print("(no callback defined) ECENT : ", msg)


def _is_running(container):
  return STR_RUNNING == container.status


def _gtk_dialog_yes_no(title, message):
  """
    Popup YES_NO dialogbox, and get result
  """
  dialog = Gtk.MessageDialog(
      flags=0,
      message_type=Gtk.MessageType.QUESTION,
      buttons=Gtk.ButtonsType.YES_NO,
      text=title,
  )
  dialog.format_secondary_text(message)
  response = dialog.run()
  result = Gtk.ResponseType.YES == response
  dialog.destroy()
  return result

def start_new_thread(target, obj, **kw):
  """
    All-in-one start new thread, w/ one argument (reference to object)
  """
  thr = threading.Thread(target=target, args=[obj], kwargs=kw)
  thr.setDaemon(True)
  thr.start()



class TheGtkTrayIndicator():
  """
    Holds everithing.
    lxd_client
    gtk stuff: indicator, notification, clipboard, menu, ...
  """

  def __init__(self, lxd_config=None):

    if lxd_config is None:
      lxd_config = {}
    self.lxd_client = pylxd.Client(**lxd_config)


    self.indicator = AppIndicator3.Indicator.new(
        APPNAME, LXD_ICON,
        AppIndicator3.IndicatorCategory.OTHER)

    self.indicator.set_status(
      AppIndicator3.IndicatorStatus.ACTIVE
    )

    self.menu = Gtk.Menu()
    self.indicator.set_menu(self.menu)
    self.indicator.set_icon_full(LXD_ICON, APPNAME)
    #self.indicator.set_attention_icon(LXD_ICON)

    Notify.init(APPNAME)
    self.notification = Notify.Notification.new('', '', None)

    self.clipboard = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)

    self.update_scheduled = False
    self.schedule_menu_update()

    self.ico_running = {}
    self.ico_running[STR_RUNNING] = Gtk.Image.new_from_file(ICO_UP)
    self.ico_running[STR_STOPPED] = Gtk.Image.new_from_file(ICO_DOWN)

### LXD ROUTINES
  def lxd_get_all(self):
    """
      Fetch all containers and coresponding status
    """
    return list(
      dict(
        name = x.name,
        is_running =  _is_running(x),
        status = x.status,
        network =  x.state().network if _is_running(x) else [],
      ) for x in self.lxd_client.containers.all()
    )

  def lxd_get_container(self, name):
    """
      Get container by name
    """
    return self.lxd_client.containers.get(name)

  def lxd_get_events_ws(self):
    """
      Get new websocket connected to event source
    """
    return self.lxd_client.events(websocket_client = MyWebSocket)
### </LXD>

  def show_notification(self, message, icon=None):
    """
      all-in-one notification popup
    """
    self.notification.update(
      "LXD Notification",
      message,
      icon if icon is not None else LXD_ICON
    )
    self.notification.show()

  def new_event(self, event):
    """
      Handle new event (should be called by event-collecting thread)
    """
    #print("NEW EVENT : ", event)

    if event['type'] == 'lifecycle':
      self.schedule_menu_update()
      # ^-- schedule update on ANY lifecycle event

      name = event['metadata']['source'].split("/")[-1]
      if event['metadata']['action'] in LXD_EVENTS_UP:
        self.show_notification(f" UP : {name}", icon = ICO_UP)
      elif event['metadata']['action'] in LXD_EVENTS_DOWN:
        self.show_notification(f" DOWN : {name}", icon = ICO_DOWN)
      else:
        self.show_notification( yaml.dump(event) )

  def _prepare_menu_for_container(self, container):
    """
      Craft menu for single container
    """
    submenu = Gtk.Menu()
    if container['is_running']:
      sub_sub_item = Gtk.MenuItem(label='Spawn shell ... ')
      sub_sub_item.connect('activate', self.click_shell)
      sub_sub_item.custom_metadata = container
      submenu.append(sub_sub_item)

      menu_sep = Gtk.SeparatorMenuItem()
      submenu.append(menu_sep)

      sub_sub_item = Gtk.MenuItem(label='STOP instance ... ')
      sub_sub_item.connect('activate', self.click_stop_instance)
      sub_sub_item.custom_metadata = container
      submenu.append(sub_sub_item)
      menu_sep = Gtk.SeparatorMenuItem()
      submenu.append(menu_sep)
      for net_name, net_value in container['network'].items():
        #print(net_value)
        for address in net_value['addresses']:
          lab = f"{net_name}/{address['family']}\t:\t{address['address']}"
          sub_sub_item = Gtk.MenuItem(label=lab)
          sub_sub_item.connect('activate', self.click_copy_address)
          sub_sub_item.custom_metadata = address
          submenu.append(sub_sub_item)
    else:
      sub_sub_item = Gtk.MenuItem(label='START instance ... ')
      sub_sub_item.connect('activate', self.click_start_instance)
      sub_sub_item.custom_metadata = container
      submenu.append(sub_sub_item)
    return submenu


  def recreate_menu(self):
    """
      Clear context menu and add all entries again ... getting fresh container satus
    """
    #clear previous
    for i in self.menu.get_children():
      self.menu.remove(i)

    # Reload button
    menuitem = Gtk.MenuItem(label='Force reload status ... ')
    menuitem.connect('activate', self.schedule_menu_update)
    self.menu.append(menuitem)

    for container in self.lxd_get_all():
      ico = self.ico_running[container['status']]
      menuitem = Gtk.ImageMenuItem.new_with_label(
        label = f"{container['name']} ({container['status']})"
      )
      menuitem.set_image( ico )
      menuitem.set_always_show_image(True)
      submenu = self._prepare_menu_for_container(container)
      menuitem.set_submenu(submenu)
      self.menu.append(menuitem)

    menu_sep = Gtk.SeparatorMenuItem()
    self.menu.append(menu_sep)

    # quit button
    item_quit = Gtk.MenuItem(label = 'Quit')
    item_quit.connect('activate', self.click_stop)
    self.menu.append(item_quit)

    self.menu.show_all()

    self.update_scheduled = False

  def schedule_menu_update(self, *_):
    if self.update_scheduled:
      return
    self.update_scheduled = True
    GLib.idle_add(self.recreate_menu, priority=GLib.PRIORITY_DEFAULT)
    #GObject.idle_add(self.recreate_menu, priority=GObject.PRIORITY_DEFAULT)

  def click_shell(self, source):
    cont = source.custom_metadata
    name = cont['name']
    sub_cmd = LXD_SHELL_COMMAND.format(name=name)
    command = TERMINAL_COMMAND.format(cmd=sub_cmd)
    print(f"Will execute : {command}")
    os.system(command)

  def click_start_instance(self, source):
    cont = source.custom_metadata
    if _gtk_dialog_yes_no("Confirm", f"START : {cont['name']}"):
      print(f"Will start ... {cont['name']} ... ")
      self.lxd_get_container(cont['name']).start()

  def click_stop_instance(self, source):
    cont = source.custom_metadata
    if _gtk_dialog_yes_no("Confirm", f"STOP : {cont['name']}" ):
      print(f"Will stop container ... {cont['name']} ... " )
      self.lxd_get_container(cont['name']).stop()

  def click_copy_address(self, source):
    data = source.custom_metadata
    addr = data['address']
    self.clipboard.set_text(addr, -1)
    self.show_notification(f"in clipboard : \n{addr}")

  def click_stop(self, _):
    Gtk.main_quit()


def periodic_update_thread(obj):
  while True:
    time.sleep(5)
    print("Update Thread BEEP")
    obj.schedule_menu_update()


def ws_event_loop_thread(obj):
  def _internal_callback(msg):
    obj.new_event(msg)

  print("Hello from WebSocket events thread !")
  wsock = obj.lxd_get_events_ws()
  wsock.callback = _internal_callback
  wsock.connect()
  wsock.run()



def main():

  parser = argparse.ArgumentParser(description=APPNAME)
  parser.add_argument('--endpoint', help="LXD endpoint address", default=None)
  parser.add_argument('--cert',     help=".crt file used to authenticate", default=None)
  parser.add_argument('--pkey',     help=".key file used to authenticate", default=None)
  parser.add_argument('--term',     help="Specify how to launch new terminal." +
  "'{cmd}' template variable holds command to spawn shell ('lxc shell [container-name]')")
  # TODO: implement this one
  #parser.add_argument('--command',nargs='+',
  #  help="Add custom command into container submenu. variable '{name}' holds container name  ")

  args = parser.parse_args()

  if args.term:
    global TERMINAL_COMMAND
    TERMINAL_COMMAND = args.term

  lxd_config = {}
  if args.endpoint is not None:
    if args.cert is None or args.pkey is None:
      raise Exception("ERROR: external endpoint require CERT + KEY to authenticate")
    lxd_config = dict(
      endpoint = args.endpoint,
      cert = (args.cert, args.pkey),
    )

  #GObject.threads_init()
  # ^-- PyGIDeprecationWarning: Since version 3.11, calling threads_init is no longer needed. See: https://wiki.gnome.org/PyGObject/Threading

  obj = TheGtkTrayIndicator(lxd_config = lxd_config)
  start_new_thread(
    target = ws_event_loop_thread,
    obj = obj,
  )
  Gtk.main()


if __name__ == "__main__":
  signal.signal(signal.SIGINT, signal.SIG_DFL)
  main()

