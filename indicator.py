
import signal
import gi
gi.require_version('Gtk', '3.0')
gi.require_version('AppIndicator3', '0.1')

from gi.repository import Gtk, AppIndicator3, GObject
import time
from threading import Thread

from datetime import datetime

from pylxd import Client
import yaml



class YetAnotherClient:

  def __init__(self,*a):
    self.client = Client(*a)


  def get_all(self):
    return dict( (x.name,x.status) for x in self.client.containers.all() )

  def get_network(self, name):
    print("GET " , name,"?!")
    return self.client.containers.get(name).state().network


import os

APPINDICATOR_ID = 'cndbappindicator'

APPNAME = "LXD-indicator"
VERSION = "1.0"

ROOTDIR = os.path.dirname(__file__)
APPDIR = os.path.realpath(ROOTDIR)
ICON = os.path.join(APPDIR, 'icons/container.svg')

ICO_UP   = os.path.join(APPDIR, 'icons/up.svg')
ICO_DOWN = os.path.join(APPDIR, 'icons/down.svg')


class Indicator():
  def __init__(self):
    self.app = 'test123'
    iconpath = "/opt/abouttime/icon/indicator_icon.png"
    self.indicator = AppIndicator3.Indicator.new(
        self.app, iconpath,
        AppIndicator3.IndicatorCategory.OTHER)
    self.indicator.set_status(AppIndicator3.IndicatorStatus.ACTIVE)       
    
    self.lxd = YetAnotherClient()

    self.about_dialog = None
    self.menu = Gtk.Menu()
    self.set_menu()

    #self.indicator.set_label("1 Monkey", self.app)
    print(ICON)
    self.indicator.set_icon(ICON)
    self.indicator.set_attention_icon(ICON)
    
    
    # the thread:
    self.update = Thread(target=self.show_seconds)
    # daemonize the thread to make the indicator stopable
    self.update.setDaemon(True)
    self.update.start()

  def set_menu(self):
    print("Set menu executed !")
    self.indicator.set_menu(self.menu)

  def recreate_menu(self):
    for i in self.menu.get_children():
      self.menu.remove(i) 

    now = datetime.now()
    dt_string = now.strftime("%d/%m/%Y %H:%M:%S")
    
    #menu item 1
    #  item_1 = Gtk.MenuItem('Menu item : ' + str(dt_string))
    #  item_1.connect('activate', self.about)
    #  menu.append(item_1)


    for key, value in self.lxd.get_all().items():
      #print(ICO_UP)
      ico = ICO_DOWN
      if "Run" in value:
        ico = ICO_UP
      item_x = Gtk.ImageMenuItem.new_with_label(key)
      item_x.set_image(Gtk.Image.new_from_file(ico))
      item_x.set_always_show_image(True)
      item_x.connect('activate', self.on_item_click)
      item_x.__metadata = key
      self.menu.append(item_x)

    menu_sep = Gtk.SeparatorMenuItem()
    self.menu.append(menu_sep)
    # quit
    item_quit = Gtk.MenuItem('Quit')
    item_quit.connect('activate', self.stop)
    self.menu.append(item_quit)

    self.menu.show_all()

  def on_item_click(self, source):
    #print(source)
    #print(dir(source))
    con = source.__metadata
    
    dialog = Gtk.MessageDialog(
        flags=0,
        message_type=Gtk.MessageType.INFO,
        buttons=Gtk.ButtonsType.OK,
        text="Info about container : " + con,
    )
    dialog.format_secondary_text(
        yaml.dump( self.lxd.get_network(con) )
    )
    dialog.run()
    dialog.destroy()




  def about(self):
    pass

  def show_seconds(self):
    while True:
        
        print("Update thread")
        GObject.idle_add(
          self.recreate_menu,
          priority=GObject.PRIORITY_DEFAULT
        )
        time.sleep(5)

  def stop(self, source):
    Gtk.main_quit()


def main():
  Indicator()
  GObject.threads_init()
  Gtk.main()


if __name__ == "__main__":
  signal.signal(signal.SIGINT, signal.SIG_DFL)
  main()
