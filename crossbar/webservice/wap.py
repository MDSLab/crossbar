#####################################################################################
#
#  Copyright (c) Crossbar.io Technologies GmbH
#
#  Unless a separate license agreement exists between you and Crossbar.io GmbH (e.g.
#  you have purchased a commercial license), the license terms below apply.
#
#  Should you enter into a separate license agreement after having received a copy of
#  this software, then the terms of such license agreement replace the terms below at
#  the time at which such license agreement becomes effective.
#
#  In case a separate license agreement ends, and such agreement ends without being
#  replaced by another separate license agreement, the license terms below apply
#  from the time at which said agreement ends.
#
#  LICENSE TERMS
#
#  This program is free software: you can redistribute it and/or modify it under the
#  terms of the GNU Affero General Public License, version 3, as published by the
#  Free Software Foundation. This program is distributed in the hope that it will be
#  useful, but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.
#
#  See the GNU Affero General Public License Version 3 for more details.
#
#  You should have received a copy of the GNU Affero General Public license along
#  with this program. If not, see <http://www.gnu.org/licenses/agpl-3.0.en.html>.
#
#####################################################################################

from __future__ import absolute_import

import os

from werkzeug.routing import Map, Rule
from werkzeug.exceptions import NotFound, MethodNotAllowed
from werkzeug.utils import escape

from jinja2 import Environment, FileSystemLoader

from txaio import make_logger

from twisted.web import resource
from twisted.web import server

from autobahn.wamp.types import ComponentConfig
from autobahn.twisted.wamp import ApplicationSession

from crossbar.webservice.base import RouterWebService

__all__ = ('RouterWebServiceWap', )


class WapResource(resource.Resource):
    """
    Twisted Web resource for WAMP Application Page web service.

    This resource uses templates loaded into jinja2.sandbox.SandboxedEnvironments
    to render HTML pages with data retrieved from a WAMP procedure call, triggered
    from the original Web request.
    """

    log = make_logger()

    isLeaf = True

    def __init__(self, worker, config, path):
        """
        :param worker: The router worker controller within this Web service is started.
        :type worker: crossbar.worker.router.RouterController

        :param config: The Web service configuration item.
        :type config: dict
        """
        resource.Resource.__init__(self)
        self._worker = worker
        self._config = config
        self._session_cache = {}

        self._realm_name = config.get('wamp', {}).get('realm', None)
        self._service_agent = worker.realm_by_name(self._realm_name).session

        #   TODO:
        #       We need to lookup the credentials for the current user based on the pre-established
        #       HTTP session cookie, this will establish the 'authrole' the user is running as.
        #       This 'authrole' can then be used to authorize the back-end topic call.
        #   QUESTION:
        #       Does the topic need the authid, if so, how do we pass it?
        #
        #   This is our default (anonymous) session for unauthenticated users
        #
        router = worker._router_factory.get(self._realm_name)
        self._default_session = ApplicationSession(ComponentConfig(realm=self._realm_name, extra=None))
        worker._router_session_factory.add(self._default_session, router, authrole='anonymous')

        # Setup Jinja2 to point to our templates folder
        #
        templates_dir = os.path.abspath(
            os.path.join(self._worker.config.extra.cbdir, config.get("templates")))
        env = Environment(loader=FileSystemLoader(templates_dir), autoescape=True)
        self.log.info(
            'WapResource is using templates directory "{templates_dir}"', templates_dir=templates_dir)

        # http://werkzeug.pocoo.org/docs/dev/routing/#werkzeug.routing.Map
        map = Map()

        # Add all our routes into 'map', note each route endpoint is a tuple of the
        # topic to call, and the template to use when rendering the results.
        for route in config.get('routes', {}):
            route_url = '/' + path + route.get('path')
            route_methods = [route.get('method')]
            route_endpoint = (route['call'], env.get_template(route['render']))
            map.add(Rule(route_url, methods=route_methods, endpoint=route_endpoint))
            self.log.info(
                'WapResource route added (url={route_url}, methods={route_methods}, endpoint={route_endpoint})',
                route_url=route_url,
                route_methods=route_methods,
                route_endpoint=route_endpoint)

        # http://werkzeug.pocoo.org/docs/dev/routing/#werkzeug.routing.MapAdapter
        # http://werkzeug.pocoo.org/docs/dev/routing/#werkzeug.routing.MapAdapter.match
        self._map_adapter = map.bind('/')

    def _after_call_success(self, result, request):
        """
        When the WAMP call attached to the URL returns, render the WAMP result
        into a Jinja2 template and return HTML to client.

        :param payload: The dict returned from the topic
        :param request: The HTTP request.
        :return: server.NOT_DONE_YET (special)
        """
        try:
            rendered_html = request.template.render(result)
        except Exception as e:
            emsg = 'WabResource render error for WAMP result of type "{}": {}'.format(type(result), e)
            self.log.warn(emsg)
            request.setResponseCode(500)
            request.write(self._render_error(emsg, request))
        else:
            request.write(rendered_html.encode('utf8'))
        request.finish()

    def _after_call_error(self, error, request):
        """
        Deferred error, write out the error template and finish the request

        :param error: The current deferred error object
        :param request: The original HTTP request
        :return: None
        """
        self.log.error('WapResource error: {error}', error=error)
        request.setResponseCode(500)
        request.write(self._render_error(error.value.error, request))
        request.finish()

    def _render_error(self, message, request):
        """
        Error renderer, display a basic error message to tell the user that there
        was a problem and roughly what the problem was.

        :param message: The current error message
        :param request: The original HTTP request
        :return: HTML formatted error string
        """
        return """
            <html>
                <title>API Error</title>
                <body>
                    <h3 style="color: #f00">Crossbar WAMP Application Page Error</h3>
                    <pre>{}</pre>
                </body>
            </html>
        """.format(escape(message)).encode('utf8')

    def render_GET(self, request):
        """
        Initiate the rendering of a HTTP/GET request by calling a WAMP procedure, the
        resulting ``dict`` is rendered together with the specified Jinja2 template
        for this URL.

        :param request: The HTTP request.
        :returns: server.NOT_DONE_YET (special)
        """
        cookie = request.received_cookies.get(b'session_cookie')
        self.log.debug('Session Cookie is ({})'.format(cookie))
        if cookie:
            session = self._session_cache.get(cookie)
            if not session:
                # FIXME: lookup role for current session
                self.log.debug('Creating a new session for cookie ({})'.format(cookie))
                authrole = 'anonymous'
                session = ApplicationSession(ComponentConfig(realm=self._realm_name, extra=None))
                self._worker._router_session_factory.add(session, authrole=authrole)
                self._session_cache[cookie] = session
            else:
                self.log.debug('Using a cached session for ({})'.format(cookie))
        else:
            self.log.debug('No session cookie, falling back on default session')
            session = self._default_session

        if not session:
            self.log.error('could not call procedure - no session')
            return self._render_error('could not call procedure - no session', request)

        full_path = request.uri.decode('utf-8')
        try:
            # werkzeug.routing.MapAdapter
            # http://werkzeug.pocoo.org/docs/dev/routing/#werkzeug.routing.MapAdapter.match
            (procedure, request.template), kwargs = self._map_adapter.match(full_path)

            self.log.debug(
                'WapResource HTTP/GET "{full_path}" mapped to procedure "{procedure}"',
                full_path=full_path,
                procedure=procedure)

            # FIXME: how do we allow calling WAMP procedures with positional args?
            if kwargs:
                d = session.call(procedure, **kwargs)
            else:
                d = session.call(procedure)

            # d.addCallback(self._after_call_success, request)
            # d.addErrback(self._after_call_error, request)
            d.addCallbacks(
                self._after_call_success,
                self._after_call_error,
                callbackArgs=[request],
                errbackArgs=[request])

            return server.NOT_DONE_YET

        except NotFound:
            request.setResponseCode(404)
            return self._render_error('path not found [werkzeug.routing.MapAdapter.match]', request)

        except MethodNotAllowed:
            request.setResponseCode(511)
            return self._render_error('method not allowed [werkzeug.routing.MapAdapter.match]', request)

        except Exception:
            request.setResponseCode(500)
            request.write(self._render_error('unknown error [werkzeug.routing.MapAdapter.match]', request))
            raise


class RouterWebServiceWap(RouterWebService):
    """
    WAMP Application Page service.
    """

    @staticmethod
    def check(personality, config):
        """
        Checks the configuration item. When errors are found, an
        InvalidConfigException exception is raised.

        :param personality: The node personality class.
        :param config: The Web service configuration item.
        :raises: crossbar.common.checkconfig.InvalidConfigException
        """
        pass

    @staticmethod
    def create(transport, path, config):
        """
        Factory to create a Web service instance of this class.

        :param transport: The Web transport in which this Web service is created on.
        :param path: The (absolute) URL path on which the Web service is to be attached.
        :param config: The Web service configuration item.
        :return: An instance of this class.
        """
        personality = transport.worker.personality
        personality.WEB_SERVICE_CHECKERS['wap'](personality, config)

        return RouterWebServiceWap(transport, path, config, WapResource(transport.worker, config, path))
