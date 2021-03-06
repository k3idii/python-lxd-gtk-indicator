
import signal
import os
import argparse
import time
import threading
import json
import yaml
from urllib import parse as url_parse

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
    # print(msg)
    if len(self.interesting_events) > 0:
      if msg['type'] not in self.interesting_events:
        return
    # this way we can handle even in external code w/o need to re-define whole class
    if self.callback is not None:
      self.callback(msg)
    else:
      print("(no callback defined) EVENT : ", msg)


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



class TheGtkTrayIndicator():
  """
    Holds everithing.
    lxd_client
    gtk stuff: indicator, notification, clipboard, menu, ...
  """
  lxd_client = None
  lxd_config = None
  menu = None
  indicator = None
  is_update_scheduled = False

  _event_thread = None
  _event_thread_condition = True
  _event_socket = None



  def __init__(self, lxd_config=None):

    self.lxd_config = lxd_config
    if lxd_config is None:
      self.lxd_config = {}
    
    self.lxd_create_client()

    self.indicator = AppIndicator3.Indicator.new(
        APPNAME, LXD_ICON,
        AppIndicator3.IndicatorCategory.OTHER
    )

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

    self.is_update_scheduled = False
    self.schedule_menu_update()

    self.ico_running = {}
    self.ico_running[STR_RUNNING] = Gtk.Image.new_from_file(ICO_UP)
    self.ico_running[STR_STOPPED] = Gtk.Image.new_from_file(ICO_DOWN)

    thr = threading.Thread(target=self._websocket_event_loop)
    thr.setDaemon(True)
    self._event_thread = thr
    thr.start()

###
### Event-socket thread 
###

  def _websocket_event_loop(self):
    def _internal_callback(msg):
      self.new_event(msg)

    while self._event_thread_condition:
      print("Hello from WebSocket events thread !")
      self._event_socket = self.lxd_get_events_ws()

      #  
      # this hack here is due to lack of Project=xxx param support in pylxd 
      #
      if self._lxd_current_project:
        resource = self._event_socket.resource
        #print("ORG resource :", resource)
        parsed = url_parse.urlparse(resource)
        org_path = parsed.path
        query = url_parse.parse_qs(parsed.query)
        query.update(dict(project = self._lxd_current_project))
        args = url_parse.urlencode(query)
        resource = f"{org_path}?{args}"
        #print("Hacked resource : ", resource)
        self._event_socket.resource = resource
        # 
        # end of hack !
        #

      self._event_socket.callback = _internal_callback
      self._event_socket.connect()
      self._event_socket.run()
      print("Well... the socket was somehow closed !")
      time.sleep(1)
      # wait 1 second, and try to reconnect to socket (in case client was restarted/etc)
      

###  
### LXD ROUTINES
###

  def lxd_create_client(self, project_name=None):
    self._lxd_current_project = project_name
    self.lxd_client = pylxd.Client(project=project_name, **self.lxd_config)
    if self._event_socket is not None:
      self._event_socket.close()


  def lxd_get_all_containers(self):
    """  Fetch all containers and coresponding status """
    return list(
      dict(
        name = x.name,
        is_running =  _is_running(x),
        status = x.status,
        network =  x.state().network if _is_running(x) else [],
      ) for x in self.lxd_client.containers.all()
    )

  def lxd_get_container(self, name):
    """ Get container by name """
    return self.lxd_client.containers.get(name)

  def lxd_get_events_ws(self):
    """ Get new websocket connected to event source """
    return self.lxd_client.events(websocket_client = MyWebSocket)

  def lxd_get_all_projects_names(self):
    return list(
      x.name for x in self.lxd_client.projects.all()
    )
  
  def lxd_full_switch_project(self, name):
    tmp = self.lxd_client.projects.get(name) 
    # ^--- this should fail if project noexists
    # previous dirty hack : self.lxd_client.api._project = name
    self.lxd_create_client(project_name=name)
  
  def lxd_get_current_project_name(self):
    return self._lxd_current_project if self._lxd_current_project else "default" 
    # ^-- handle "none"
    #proj = self.lxd_client.api._project
    
### 
### </LXD>
###

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

      name = event['metadata']['source'].split("/")[-1].split("?")[0] # huh dirty:)
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

      submenu.append(Gtk.SeparatorMenuItem())

      sub_sub_item = Gtk.MenuItem(label='STOP instance ... ')
      sub_sub_item.connect('activate', self.click_stop_instance)
      sub_sub_item.custom_metadata = container
      submenu.append(sub_sub_item)

      submenu.append(Gtk.SeparatorMenuItem())
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
    menuitem.connect('activate', self.click_update)
    self.menu.append(menuitem)
    
    self.menu.append(Gtk.SeparatorMenuItem())
    
    submenu = Gtk.Menu()
    for proj in self.lxd_get_all_projects_names():
      menuitem = Gtk.MenuItem(label=f"Switch to : {proj}")
      menuitem.connect("activate", self.click_switch_project)
      menuitem.custom_metadata = proj
      submenu.append(menuitem)
    menuitem = Gtk.MenuItem(label=f"Project:\t {self.lxd_get_current_project_name()}")
    menuitem.set_submenu(submenu)
    self.menu.append(menuitem)


    self.menu.append(Gtk.SeparatorMenuItem())
    
    for container in self.lxd_get_all_containers():
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

    self.is_update_scheduled = False

  def schedule_menu_update(self):
    if self.is_update_scheduled:
      return
    self.is_update_scheduled = True
    GLib.idle_add(self.recreate_menu, priority=GLib.PRIORITY_DEFAULT)
    #GObject.idle_add(self.recreate_menu, priority=GObject.PRIORITY_DEFAULT)

###
### CLICK HANDLERS
###

  def click_update(self, _):
    self.schedule_menu_update()

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

  def click_switch_project(self, source):
    proj = source.custom_metadata
    self.show_notification(f"Switch to project : {proj}")
    self.lxd_full_switch_project(proj)
    self.schedule_menu_update()


  def click_stop(self, _):
    #self.lxd_client.close()
    self.event_socket.close()
    #Gtk.main_quit()


def _unused_yet_periodic_update_thread(obj):
  while True:
    time.sleep(5)
    print("Update Thread BEEP")
    obj.schedule_menu_update()



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
  Gtk.main()


if __name__ == "__main__":
  signal.signal(signal.SIGINT, signal.SIG_DFL)
  main()

