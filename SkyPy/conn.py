import os
import functools
import datetime
import time
import math
import hashlib

import bs4
import requests

class SkypeConnection(object):
    """
    The main connection class -- handles all requests to API resources.

    An instance of this class is callable, and performs an API request.  Arguments are similar to the underlying requests library.
    """
    class Auth:
        """
        Enum for authentication types.  Skype uses X-SkypeToken, whereas Reg includes RegistrationToken.
        """
        Skype, Reg = range(2)
    API_LOGIN = "https://login.skype.com/login?client_id=578134&redirect_uri=https%3A%2F%2Fweb.skype.com"
    API_USER = "https://api.skype.com"
    API_CONTACTS = "https://contacts.skype.com/contacts/v1"
    API_MSGSHOST = "https://client-s.gateway.messenger.live.com/v1/users/ME"
    @staticmethod
    def resubscribeOn(*codes):
        """
        Decorator: if a given status code is received, try resubscribing to avoid the error.
        """
        def decorator(func):
            @functools.wraps(func)
            def wrapper(self, *args, **kwargs):
                try:
                    return func(self, *args, **kwargs)
                except SkypeApiException as e:
                    if len(e.args) >= 2 and isinstance(e.args[1], requests.Response) and e.args[1].status_code in codes:
                        self.conn.getRegToken()
                        self.conn.subscribe()
                        return func(self, *args, **kwargs)
                    else:
                        raise e
                except requests.exceptions.ConnectionError:
                    self.conn.getRegToken()
                    self.conn.subscribe()
                    return func(self, *args, **kwargs)
            return wrapper
        return decorator
    def __init__(self, user=None, pwd=None, tokenFile=None):
        self.tokens = {}
        self.tokenExpiry = {}
        if tokenFile and os.path.isfile(tokenFile):
            with open(tokenFile, "r") as f:
                skypeToken, skypeExpiry, msgsHost = f.read().splitlines()
                skypeExpiry = datetime.datetime.fromtimestamp(int(skypeExpiry))
                if datetime.datetime.now() < skypeExpiry:
                    self.tokens["skype"] = skypeToken
                    self.tokenExpiry["skype"] = skypeExpiry
                    self.msgsHost = msgsHost
        if not self.tokens:
            self.login(user, pwd)
            self.msgsHost = self.API_MSGSHOST
            if tokenFile:
                with open(tokenFile, "w") as f:
                    f.write(self.tokens["skype"] + "\n")
                    f.write(str(int(time.mktime(self.tokenExpiry["skype"].timetuple()))) + "\n")
                    f.write(self.msgsHost + "\n")
        self.getRegToken()
        self.makeEndpoint()
        self.subscribe()
    def __call__(self, method, url, codes=[200, 201, 207], auth=None, headers={}, params=None, data=None, json=None):
        """
        Make an API call.  Most parameters are passed directly to requests.

        Set codes to a list of valid HTTP response codes -- an exception is raised if the response does not match.

        If authentication is required, set auth to one of the SkypeConnection.Auth constants.
        """
        if auth == self.Auth.Skype:
            headers["X-SkypeToken"] = self.tokens["skype"]
        elif auth == self.Auth.Reg:
            headers["RegistrationToken"] = self.tokens["reg"]
        resp = requests.request(method, url, headers=headers, params=params, data=data, json=json)
        if resp.status_code not in codes:
            raise SkypeApiException("{0} response from {1} {2}".format(resp.status_code, method, url), resp)
        return resp
    def login(self, user, pwd):
        """
        Scrape the Skype Web login page, and perform a login with the given username and password.
        """
        loginPage = bs4.BeautifulSoup(self("GET", self.API_LOGIN).text, "html.parser")
        if loginPage.find(id="recaptcha_response_field"):
            raise SkypeApiException("Captcha required")
        pie = loginPage.find(id="pie").get("value")
        etm = loginPage.find(id="etm").get("value")
        secs = int(time.time())
        frac, hour = math.modf(time.timezone)
        timezone = "{0:+03d}|{1}".format(int(hour), int(frac * 60))
        loginResp = self("POST", self.API_LOGIN, data={
            "username": user,
            "password": pwd,
            "pie": pie,
            "etm": etm,
            "timezone_field": timezone,
            "js_time": secs
        })
        loginRespPage = bs4.BeautifulSoup(loginResp.text, "html.parser")
        errors = loginRespPage.select("div.messageBox.message_error span")
        if errors:
            raise SkypeApiException("Error message returned", loginResp, errors[0].text)
        try:
            self.tokens["skype"] = loginRespPage.find("input", {"name": "skypetoken"}).get("value")
            self.tokenExpiry["skype"] = datetime.datetime.fromtimestamp(secs + int(loginRespPage.find("input", {"name": "expires_in"}).get("value")))
        except AttributeError as e:
            raise SkypeApiException("Couldn't retrieve Skype token from login response", loginResp)
    def getRegToken(self):
        """
        Acquire a registration token.  See getMac256Hash(...) for the hash generation.
        """
        secs = int(time.time())
        endpointResp = self("POST", self.msgsHost + "/endpoints", codes=[201, 301], headers={
            "LockAndKey": "appId=msmsgs@msnmsgr.com; time=" + str(secs) + "; lockAndKeyResponse=" + getMac256Hash(str(secs), "msmsgs@msnmsgr.com", "Q1P7W2E4J9R8U3S5"),
            "Authentication": "skypetoken=" + self.tokens["skype"]
        }, json={})
        location = endpointResp.headers["Location"].rsplit("/", 2)[0]
        regTokenHead = endpointResp.headers["Set-RegistrationToken"]
        if not location == self.msgsHost:
            self.msgsHost = location
            return self.getRegToken()
        self.tokens["reg"] = regTokenHead
    def makeEndpoint(self):
        endResp = self("POST", self.msgsHost + "/endpoints", auth=self.Auth.Reg, json={})
        self.msgsEndpoint = endResp.headers["Location"]
        self("PUT", self.msgsEndpoint + "/presenceDocs/messagingService", auth=self.Auth.Reg, json={
            "id": "messagingService",
            "privateInfo": {
                "epname": "skype"
            },
            "publicInfo": {
                "capabilities": "",
                "nodeInfo": "xx",
                "skypeNameVersion": "skype.com",
                "type": 1
            },
            "selfLink": "uri",
            "type": "EndpointPresenceDoc"
        })
    def subscribe(self):
        """
        Subscribe to contact and conversation events.  These are accessible through Skype.getEvents().
        """
        self("POST", self.msgsHost + "/endpoints/SELF/subscriptions", auth=self.Auth.Reg, json={
            "interestedResources": [
                "/v1/threads/ALL",
                "/v1/users/ME/contacts/ALL",
                "/v1/users/ME/conversations/ALL/messages",
                "/v1/users/ME/conversations/ALL/properties"
            ],
            "template": "raw",
            "channelType": "httpLongPoll"
        })
    def __str__(self):
        return "[{0}]".format(self.__class__.__name__)
    def __repr__(self):
        return "{0}()".format(self.__class__.__name__)

class SkypeApiException(Exception):
    """
    A generic Skype-related exception.
    """
    pass

def getMac256Hash(challenge, appId, key):
    def int32ToHexString(n):
        hexChars = "0123456789abcdef"
        hexString = ""
        for i in range(4):
            hexString += hexChars[(n >> (i * 8 + 4)) & 15]
            hexString += hexChars[(n >> (i * 8)) & 15]
        return hexString
    def int64Xor(a, b):
        sA = "{0:b}".format(a)
        sB = "{0:b}".format(b)
        sC = ""
        sD = ""
        diff = abs(len(sA) - len(sB))
        for i in range(diff):
            sD += "0"
        if len(sA) < len(sB):
            sD += sA
            sA = sD
        elif len(sB) < len(sA):
            sD += sB
            sB = sD
        for i in range(len(sA)):
            sC += "0" if sA[i] == sB[i] else "1"
        return int(sC, 2)
    def cS64(pdwData, pInHash):
        if len(pdwData) < 2 or len(pdwData) & 1 == 1:
            return None
        MODULUS = 2147483647
        CS64_a = pInHash[0] & MODULUS
        CS64_b = pInHash[1] & MODULUS
        CS64_c = pInHash[2] & MODULUS
        CS64_d = pInHash[3] & MODULUS
        CS64_e = 242854337
        pos = 0
        qwDatum = 0
        qwMAC = 0
        qwSum = 0
        for i in range(len(pdwData) // 2):
            qwDatum = int(pdwData[pos])
            pos += 1
            qwDatum *= CS64_e
            qwDatum = qwDatum % MODULUS
            qwMAC += qwDatum
            qwMAC *= CS64_a
            qwMAC += CS64_b
            qwMAC = qwMAC % MODULUS
            qwSum += qwMAC
            qwMAC += int(pdwData[pos])
            pos += 1
            qwMAC *= CS64_c
            qwMAC += CS64_d
            qwMAC = qwMAC % MODULUS
            qwSum += qwMAC
        qwMAC += CS64_b
        qwMAC = qwMAC % MODULUS
        qwSum += CS64_d
        qwSum = qwSum % MODULUS
        return [qwMAC, qwSum]
    clearText = challenge + appId
    clearText += "0" * (8 - len(clearText) % 8)
    cchClearText = len(clearText) // 4
    pClearText = []
    for i in range(cchClearText):
        pClearText = pClearText[:i] + [0] + pClearText[i:]
        for pos in range(4):
            pClearText[i] += ord(clearText[pos]) * (256 ** pos)
    sha256Hash = [0, 0, 0, 0]
    hash = hashlib.sha256((challenge + key).encode("utf-8")).hexdigest().upper()
    for i in range(len(sha256Hash)):
        sha256Hash[i] = 0
        for pos in range(4):
            dpos = pos * 2
            sha256Hash[i] += int(hash[dpos:dpos + 2], 16) * (256 ** pos)
    macHash = cS64(pClearText, sha256Hash)
    macParts = [macHash[0], macHash[1], macHash[0], macHash[1]]
    return "".join(map(int32ToHexString, map(int64Xor, sha256Hash, macParts)))
