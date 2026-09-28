import smtplib, ssl
from settingshandler import readSettings


def getSmtpSettings():
    settings = readSettings()
    return settings["smtp"]


def sendEmail(subject, message):
    smtpSettings = getSmtpSettings()
    content = f"Subject: {subject}\n\n{message}"

    try:
        context = ssl.create_default_context()
        with smtplib.SMTP_SSL(smtpSettings["server"], smtpSettings["port"], context=context) as server:
            server.login(smtpSettings["sender"], smtpSettings["password"])
            server.sendmail(smtpSettings["sender"], smtpSettings["recipient"], content)
        return None
    except Exception as e:
        return f"Sending email failed: {e}"



if __name__ == "__main__":
    sendEmail("Test", "My test message")
