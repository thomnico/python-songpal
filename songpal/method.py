import asyncio
import json
import logging
from pprint import pformat as pf

import attr
import aiohttp

from .common import SongpalException, ProtocolType
from .containers import (PowerChange, VolumeChange, SettingChange,
                         SoftwareUpdateChange, ContentChange,
                         NotificationChange)

_LOGGER = logging.getLogger(__name__)


@attr.s
class Signature:
    name = attr.ib()
    input = attr.ib()
    output = attr.ib()
    version = attr.ib()


class Method:
    """ A Method represents a single API method.

    Internally these are called APIs.
    """
    def __init__(self, service, endpoint,
                 payload, signature, protocol,
                 idgen, debug=0):
        self.versions = payload["versions"]
        name = payload["name"]

        self.name = name
        self.debug = debug
        self.service = service
        self.endpoint = endpoint
        self.signature = signature
        self.protocol = protocol
        self.idgen = idgen

        self.timeout = 2

        self._inputs = self.parse_inputs(self.signature)
        self._outputs = self.parse_outputs(self.signature)
        self.version = self.signature.version

    def asdict(self):
        return {'name': self.name,
                'service': self.service,
                'endpoint': self.endpoint,
                'signature': attr.asdict(self.signature),
                'inputs': self.serialize_types(self.inputs),
                'outputs': self.serialize_types(self.outputs),
                'version': self.version}

    def wrap_notification(self, data):
        if "method" in data:
            method = data["method"]
            params = data["params"]
            change = params[0]
            if method == "notifyPowerStatus":
                return PowerChange.make(**change)
            elif method == "notifyVolumeInformation":
                return VolumeChange.make(**change)
            elif method == "notifyPlayingContentInfo":
                return ContentChange.make(**change)
            elif method == "notifySettingsUpdate":
                return SettingChange.make(**change)
            elif method == "notifySWUpdateInfo":
                return SoftwareUpdateChange.make(**change)
            else:
                _LOGGER.warning("Got unknown notification type: %s", method)
        elif "result" in data:
            result = data["result"][0]
            if "enabled" in result and "enabled" in result:
                return NotificationChange(**result)
        else:
            _LOGGER.warning("Unknown notification, returning raw: %s", data)
            return data

    async def request(self, *args, **kwargs):
        _LOGGER.debug("%s got called with args (%s) kwargs (%s)" % (
            self.name, args, kwargs))

        # Used for allowing keeping reading from the socket
        _consumer = None
        if '_consumer' in kwargs:
            if self.protocol != ProtocolType.WebSocket:
                raise SongpalException("Notifications are only supported over websockets")
            _consumer = kwargs['_consumer']
            del kwargs['_consumer']

        if len(kwargs) == 0 and len(args) == 0:
            params = []  # params need to be empty array, if none is given
        elif len(kwargs) > 0:
            params = [kwargs]
        elif len(args) == 1 \
                and args[0] is not None:
            params = [args[0]]
        else:
            params = []

        # TODO check for type correctness
        # TODO note parameters are not always necessary, see getPlaybackModeSettings
        # which signatures to need 'target' and 'uri' but works just fine without anything
        #if len(params) != len(self._inputs):
        #    _LOGGER.error("args: %s signature: %s" % (args,
        #                                              self.signature.input))
        #    raise Exception("Invalid number of inputs, wanted %s got %s / %s" % (
        #        len(self.signature.input), len(args), len(kwargs)))

        async with aiohttp.ClientSession() as session:
            req = {"method": self.name,
                   "params": params,
                   "version": self.version,
                   "id": next(self.idgen)}
            if self.debug > 1:
                _LOGGER.debug("sending request: %s (proto: %s)",
                              req, self.protocol)
            if self.protocol == ProtocolType.WebSocket:
                async with session.ws_connect(self.endpoint,
                                              timeout=self.timeout,
                                              heartbeat=self.timeout * 5) as s:
                    await s.send_json(req)
                    # If we have a consumer, we are going to loop forever while
                    # emiting the incoming payloads to e.g. notification handler.
                    if _consumer is not None:
                        while True:
                            res_raw = await s.receive_json()
                            res = self.wrap_notification(res_raw)
                            _LOGGER.debug("Got notification: %s", res)
                            if self.debug > 1:
                                _LOGGER.debug("Got notification raw: %s", res_raw)

                            await _consumer(res)

                    res = await s.receive_json()
                    return res
            else:
                res = await session.post(self.endpoint, json=req)
                return await res.json()

    async def __call__(self, *args, **kwargs):
        try:
            res = await self.request(*args, **kwargs)
        except Exception as ex:
            raise SongpalException("Unable to make a request: %s" % ex) from ex

        if self.debug > 1:
            _LOGGER.debug("got payload: %s" % res)

        if "error" in res:
            _LOGGER.debug(self)
            raise SongpalException("Got an error for %s: %s" % (self.name,
                                                                res["error"]),
                                   error=res["error"])

        if self.debug > 0:
            _LOGGER.debug("got res: %s" % pf(res))

        if 'result' not in res:
            _LOGGER.error("No result in response, how to handle? %s" % res)
            return

        res = res["result"]
        if len(res) > 1:
            _LOGGER.warning("Got a response with len >  1: %s" % res)
            return res
        elif len(res) < 1:
            _LOGGER.debug("Got no response, assuming success")
            return True

        return res[0]

    def parse_inputs(self, sig):
        if len(sig.input) == 0:
            return None
        # _LOGGER.debug("%s: parsing inputs: %s" % (self.name, sig.input))
        ins = self.parse_json_sig(sig.input.pop())
        _LOGGER.debug("%s.%s ins: %s" % (self.service, self.name, ins))
        return ins

    @property
    def inputs(self):
        return self._inputs

    def return_type(self, x):
        if x == "string":
            return str
        if x == "Boolean":
            return bool
        if x == "int":
            return int

        return x

    def serialize_types(self, x):
        if x is None:
            return x

        def serialize(x):
            if isinstance(x, str):
                return x
            return x.__name__

        if isinstance(x, dict):
            serialized_dict = {k: serialize(v) for k, v in x.items()}
            return serialized_dict
        return serialize(x)

    def parse_json_sig(self, x):
        try:
            # _LOGGER.debug("trying to parse %s, len: %s" % (x, len(x)))
            if x.endswith("*"):  # TODO handle arrays properly
                # _LOGGER.debug("got an array %s: %s" % (self.name, x))
                x = x.rstrip("*")

            obj = json.loads(x)
            obj = {x: self.return_type(obj[x]) for x in obj}
        except json.JSONDecodeError as ex:
            try:
                return self.return_type(x)
            except Exception:
                raise SongpalException("Unknown return type: %s" % x) from ex

        return obj

    def parse_outputs(self, sig):
        if len(sig.output) == 0:
            return None
        # _LOGGER.debug("%s parsing outs: %s" % (self.name, sig.output))
        outs = self.parse_json_sig(sig.output.pop())
        _LOGGER.debug("%s.%s outs: %s" % (self.service, self.name, outs))
        return outs

    @property
    def outputs(self):
        return self._outputs

    def __repr__(self):
        return "<Method %s.%s(%s) -> %s>" % (self.service,
                                             self.name,
                                             pf(self.inputs),
                                             pf(self.outputs))
