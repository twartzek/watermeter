import time
import subprocess


class Led():
    def __init__(self, gpio:int):
        self.gpio = gpio
        try:
            subprocess.run(["pinctrl", str(self.gpio), "op", "pd"]) 
        except:
            pass
    
    def blink(self, on:float=0.5, off:float=0.5):
        try:
            subprocess.run(["pinctrl", str(self.gpio), "dh"]) 
            time.sleep(on)
            subprocess.run(["pinctrl", str(self.gpio), "dl"])
            time.sleep(off)
        except:
            pass

    def on(self):
        try:
            subprocess.run(["pinctrl", str(self.gpio), "dh"]) 
        except:
            pass

    def off(self):
        try:
            subprocess.run(["pinctrl", str(self.gpio), "dl"]) 
        except:
            pass

    def fastblink(self, n:int=1):
        for i in range(n):
            self.blink(0.1,0.3)