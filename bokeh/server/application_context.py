''' Provides the ``ApplicationContext`` class.

'''
from __future__ import absolute_import

import logging
log = logging.getLogger(__name__)

from tornado import gen

from .session import ServerSession
from .exceptions import ProtocolError

from bokeh.application.application import ServerContext, SessionContext
from bokeh.document import Document
from bokeh.util.tornado import _CallbackGroup, yield_for_all_futures

class BokehServerContext(ServerContext):
    def __init__(self, application_context):
        self.application_context = application_context
        self._callbacks = _CallbackGroup(self.application_context.io_loop)

    def _remove_all_callbacks(self):
        self._callbacks.remove_all_callbacks()

    @property
    def sessions(self):
        result = []
        for session in self.application_context.sessions:
            result.append(session.session_context)
        return result

    @property
    def develop_mode(self):
        return self.application_context.develop

    # TODO rename these add_next_tick
    def add_callback(self, callback):
        self._callbacks.add_next_tick_callback(callback)

    def remove_callback(self, callback):
        self._callbacks.remove_next_tick_callback(callback)

    def add_timeout_callback(self, callback, timeout_milliseconds):
        self._callbacks.add_timeout_callback(callback, timeout_milliseconds)

    def remove_timeout_callback(self, callback):
        self._callbacks.remove_timeout_callback(callback)

    def add_periodic_callback(self, callback, period_milliseconds):
        self._callbacks.add_periodic_callback(callback, period_milliseconds)

    def remove_periodic_callback(self, callback):
        self._callbacks.remove_periodic_callback(callback)

class BokehSessionContext(SessionContext):
    def __init__(self, session_id, server_context, document):
        self._document = document
        self._session = None
        super(BokehSessionContext, self).__init__(server_context,
                                                  session_id)

    def _set_session(self, session):
        self._session = session

    @gen.coroutine
    def with_locked_document(self, func):
        if self._session is None:
            # this means we are in on_session_created, so no locking yet,
            # we have exclusive access
            yield yield_for_all_futures(func(self._document))
        else:
            self._session.with_document_locked(func, self._document)

    @property
    def destroyed(self):
        if self._session is None:
            # this means we are in on_session_created
            return False
        else:
            return self._session.destroyed

class ApplicationContext(object):
    ''' Server-side holder for bokeh.application.Application plus any associated data.
        This holds data that's global to all sessions, while ServerSession holds
        data specific to an "instance" of the application.
    '''

    def __init__(self, application, develop=False, io_loop=None):
        self._application = application
        self._develop = develop
        self._loop = io_loop
        self._sessions = dict()
        self._pending_sessions = dict()
        self._session_contexts = dict()
        self._server_context = BokehServerContext(self)

    @property
    def io_loop(self):
        return self._loop

    @property
    def application(self):
        return self._application

    @property
    def develop(self):
        return self._develop

    @property
    def server_context(self):
        return self._server_context

    @property
    def sessions(self):
        return self._sessions.values()

    def run_load_hook(self):
        try:
            result = self._application.on_server_loaded(self._server_context)
            if isinstance(result, gen.Future):
                log.error("on_server_loaded returned a Future; this doesn't make sense "
                          "because we run this hook before starting the IO loop.")
        except Exception as e:
            log.error("Error in server loaded hook %r", e, exc_info=True)

    def run_unload_hook(self):
        try:
            result = self._application.on_server_unloaded(self._server_context)
            if isinstance(result, gen.Future):
                log.error("on_server_unloaded returned a Future; this doesn't make sense "
                          "because we stop the IO loop right away after calling on_server_unloaded.")
        except Exception as e:
            log.error("Error in server unloaded hook %r", e, exc_info=True)

        self._server_context._remove_all_callbacks()

    @gen.coroutine
    def create_session_if_needed(self, session_id):
        # this is because empty session_ids would be "falsey" and
        # potentially open up a way for clients to confuse us
        if len(session_id) == 0:
            raise ProtocolError("Session ID must not be empty")

        if session_id not in self._sessions and \
           session_id not in self._pending_sessions:
            future = self._pending_sessions[session_id] = gen.Future()

            doc = Document()

            session_context = BokehSessionContext(session_id,
                                                  self._server_context,
                                                  doc)
            try:
                result = yield yield_for_all_futures(self._application.on_session_created(session_context))
            except Exception as e:
                log.error("Failed to run session creation hooks %r", e, exc_info=True)

            self._application.initialize_document(doc)

            session = ServerSession(session_id, doc, io_loop=self._loop)
            del self._pending_sessions[session_id]
            self._sessions[session_id] = session
            session_context._set_session(session)
            self._session_contexts[session_id] = session_context

            # notify anyone waiting on the pending session
            future.set_result(session)

        if session_id in self._pending_sessions:
            # another create_session_if_needed is working on
            # creating this session
            session = yield self._pending_sessions[session_id]
        else:
            session = self._sessions[session_id]

        raise gen.Return(session)

    def get_session(self, session_id):
        if session_id in self._sessions:
            session = self._sessions[session_id]
            return session
        else:
            raise ProtocolError("No such session " + session_id)

    @gen.coroutine
    def _discard_session(self, session, should_discard):
        if session.connection_count > 0:
            raise RuntimeError("Should not be discarding a session with open connections")
        log.debug("Discarding session %r last in use %r seconds ago", session.id, session.seconds_since_last_unsubscribe)

        # session lifecycle hooks are supposed to be called outside the document lock
        try:
            yield yield_for_all_futures(self._application.on_session_destroyed(self._session_contexts[session.id]))
        except Exception as e:
            log.error("Failed to run session destroy hooks %r", e, exc_info=True)

        # session.destroy() wants the document lock so it can shut down the document
        # callbacks.
        def do_discard():
            # while we yielded above, the discard-worthiness of the session may have changed
            if should_discard(session):
                session.destroy()
                del self._sessions[session.id]
                del self._session_contexts[session.id]
            else:
                log.debug("Session %r was scheduled to discard but came back to life", session.id)
        yield session.with_document_locked(do_discard)

        raise gen.Return(None)

    @gen.coroutine
    def cleanup_sessions(self, unused_session_linger_seconds):
        def should_discard(session):
            return session.connection_count == 0 and \
                (session.seconds_since_last_unsubscribe > unused_session_linger_seconds or \
                 session.expiration_requested)
        # build a temp list to avoid trouble from self._sessions changes
        to_discard = []
        for session in self._sessions.values():
            if should_discard(session):
                to_discard.append(session)
        # asynchronously reconsider each session
        for session in to_discard:
            if should_discard(session):
                yield self._discard_session(session, should_discard)

        raise gen.Return(None)
