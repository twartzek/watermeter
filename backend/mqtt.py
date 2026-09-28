import paho.mqtt.publish as publish
from settingshandler import readSettings
from mylog import Logger

logger = Logger("mqtt")

settings = readSettings()


def mqtt_publish(topic, payload):
    auth = {"username": settings["mqtt"]["username"], "password": settings["mqtt"]["password"]}
    try:
        publish.single(topic=topic, payload=payload, hostname=settings["mqtt"]["broker"], port=settings["mqtt"]["port"], auth=auth, qos=1)
        logger.logger.info("Published to " + topic + ": " + str(payload))
    except Exception as e:
        logger.logger.error(e)

# if __name__ == "__main__":
#     mqtt_publish("watermeter/totalconsumption", 1)