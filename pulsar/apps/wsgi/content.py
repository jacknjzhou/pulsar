'''The :mod:`pulsar.apps.wsgi.content` introduces several utility classes for
handling asynchronous content within a :ref:`WSGI handler <wsgi-async> or
:ref:`middleware <wsgi-middleware>`.

These classes can operate instead or in conjunction with a template engine,
their main purpose is to do what a web framework does: to provide a set of
tools working together to concatenate ``strings`` to return as a
response to an :ref:`HTTP client request <app-wsgi-request>`.

A string can be ``html``, ``json``, ``plain text`` or any other valid HTTP
content type.

The main class of this module is the :class:`AsyncString`, which can be
considered as the atomic component of an asynchronous web framework::

    >>> from pulsar.apps.wsgi import AsyncString
    >>> string = AsyncString('Hello')
    >>> string.render()
    'Hello'
    >>> string.render()
    ...
    RuntimeError: AsyncString already streamed

An :class:`AsyncString` can only be rendered once, and it accepts
:ref:`asynchronous components  <tutorials-coroutine>`::

    >>> a = Future()
    >>> string = AsyncString('Hello, ', a)
    >>> value = string.render()
    >>> value
    MultiFuture (pending)
    >>> value.done()
    False

Once the future is done, we have the concatenated string::

    >>> a.set_result('World!')
    'World!'
    >>> value.done()
    True
    >>> value.result()
    'Hello, World!'

Design
===============

The :meth:`~AsyncString.do_stream` method is responsible for the streaming
of ``strings`` or :ref:`asynchronous components  <tutorials-coroutine>`.
It can be overwritten by subclasses to customise the way an
:class:`AsyncString` streams its :attr:`~AsyncString.children`.

On the other hand, the :meth:`~AsyncString.to_string` method is responsible
for the concatenation of ``strings`` and, like :meth:`~AsyncString.do_stream`,
it can be customised by subclasses.


Asynchronous String
=====================

.. autoclass:: AsyncString
   :members:
   :member-order: bysource

Asynchronous Json
=====================

.. autoclass:: Json
   :members:
   :member-order: bysource

.. _wsgi-html:

Asynchronous Html
=====================

.. autoclass:: Html
   :members:
   :member-order: bysource

.. _wsgi-html-document:

Html Document
==================

The :class:`.HtmlDocument` class is a python representation of an
`HTML5 document`_, the latest standard for HTML.
It can be used to build a web site page in a pythonic fashion rather than
using template languages::

    >>> from pulsar.apps.wsgi import HtmlDocument

    >>> doc = HtmlDocument(title='My great page title')
    >>> doc.head.add_meta(name="description", content=...)
    >>> doc.head.scripts.append('jquery')
    ...
    >>> doc.body.append(...)


Document
~~~~~~~~~~~~~~~~~~~~~~~

.. autoclass:: HtmlDocument
   :members:
   :member-order: bysource

.. _wsgi-html-head:

Head
~~~~~~~~~~

.. autoclass:: Head
   :members:
   :member-order: bysource

Media
~~~~~~~~~~

.. autoclass:: Media
   :members:
   :member-order: bysource

Scripts
~~~~~~~~~~

.. autoclass:: Scripts
   :members:
   :member-order: bysource

Links
~~~~~~~~~~

.. autoclass:: Links
   :members:
   :member-order: bysource

Html Factory
=================

.. autofunction:: html_factory


.. _`HTML5 document`: http://www.w3schools.com/html/html5_intro.asp
'''
import os
import json as pyjson

from collections import Mapping, OrderedDict
from functools import partial
from inspect import isgenerator

from pulsar import multi_async, async, coroutine_return, chain_future
from pulsar.utils.pep import iteritems, to_string, ispy3k
from pulsar.utils.slugify import slugify
from pulsar.utils.html import INLINE_TAGS, escape, dump_data_value, child_tag
from pulsar.utils.httpurl import remove_double_slash
from pulsar.utils.system import json

from .html import html_visitor

__all__ = ['AsyncString', 'Html',
           'Json', 'HtmlDocument', 'Links', 'Scripts', 'Media',
           'html_factory', 'media_libraries']


_libs_url = 'http://quantmind.github.io/jslibs/libs.json'
_media_libraries = None


def media_libraries():
    global _media_libraries
    if _media_libraries is None:
        if os.path.isfile('libs.json'):
            with open('libs.json') as f:   # pragma    nocover
                data = f.read()
            _media_libraries = json.loads(data)
        else:
            from pulsar import new_event_loop
            from pulsar.apps.http import HttpClient
            http = HttpClient(loop=new_event_loop())
            try:
                response = http.get(_libs_url)
                _media_libraries = response.json()
            except Exception:   # pragma    nocover
                _media_libraries = {'libs': {}, 'deps': {}}
    return _media_libraries


if ispy3k:

    def stream_to_string(stream):
        for value in stream:
            if value is None:
                continue
            elif isinstance(value, bytes):
                yield value.decode('utf-8')
            elif isinstance(value, str):
                yield value
            else:
                yield str(value)

else:  # pragma nocover

    def stream_to_string(stream):
        for value in stream:
            if value is None:
                continue
            elif isinstance(value, unicode):
                yield value
            else:
                yield str(value)


def stream_mapping(value, request=None):
    result = {}
    for key, value in iteritems(value):
        if isinstance(value, AsyncString):
            value = value.render(request)
        result[key] = value
    return multi_async(result)


def attr_iter(attrs):
    for k in sorted(attrs):
        v = attrs[k]
        if v is not None:
            yield " %s='%s'" % (k, escape(v, force=True))


class AsyncString(object):
    '''An asynchronous string which can be used with pulsar WSGI servers.
    '''
    _default_content_type = 'text/plain'
    _content_type = None
    '''Content type for this :class:`AsyncString`'''
    _streamed = False
    _children = None
    _parent = None
    charset = None

    def __init__(self, *children, **params):
        for child in children:
            self.append(child)
        self._setup(**params)

    def _setup(self, content_type=None, charset=None, **kw):
        self._content_type = content_type or self._default_content_type
        self.charset = charset or 'utf-8'

    @property
    def content_type(self):
        return '%s; charset=%s' % (self._content_type, self.charset)

    @property
    def parent(self):
        '''The :class:`AsyncString` element which contains this
        :class:`AsyncString`.'''
        return self._parent

    @property
    def children(self):
        '''A copy of all children of this :class:`AsyncString`.

        Children can be other :class:`AsyncString` or string or bytes,
        depending on implementation.
        :attr:`children` are added and removed via the :meth:`append` and
        :meth:`remove` methods.
        '''
        if self._children is None:
            self._children = []
        return self._children

    @property
    def has_default_content_type(self):
        '''``True`` if this is as the default content type.
        '''
        return self._content_type == self._default_content_type

    def __repr__(self):
        return self.__class__.__name__

    def __str__(self):
        return self.__repr__()

    def append(self, child):
        '''Append ``child`` to the list of :attr:`children`.

        :param child: String, bytes or another :class:`.AsyncString`.
            If it is an :class:`.AsyncString`, this instance will be
            set as its :attr:`parent`.
            If ``child`` is ``None``, this method does nothing.

        '''
        self.insert(None, child)

    def prepend(self, child):
        '''Prepend ``child`` to the list of :attr:`children`.

        This is a shortcut for the :meth:`insert` method at index 0.

        :param child: String, bytes or another :class:`AsyncString`.
            If it is an :class:`.AsyncString`, this instance will be set
            as its :attr:`parent`.
            If ``child`` is ``None``, this method does nothing.
        '''
        self.insert(0, child)

    def insert(self, index, child):
        '''Insert ``child`` into the list of :attr:`children` at ``index``.

        :param index: The index (positive integer) where to insert ``child``.
        :param child: String, bytes or another :class:`AsyncString`.
            If it is an :class:`.AsyncString`, this instance will be set as
            its :attr:`parent`.
            If ``child`` is ``None``, this method does nothing.
        '''
        # make sure that child is not in child
        if child not in (None, self):
            if isinstance(child, AsyncString):
                child_parent = child._parent
                if self._parent is child:
                    # the parent is the child we are appending.
                    # remove from the child
                    child.remove(self)
                    if child_parent:
                        index = child_parent.children.index(child)
                        child_parent.remove(child)
                        child_parent.insert(index, self)
                elif child_parent:
                    child_parent.remove(child)
                child._parent = self
            if index is None:
                self.children.append(child)
            else:
                self.children.insert(index, child)

    def remove(self, child):
        '''Remove a ``child`` from the list of :attr:`children`.'''
        try:
            self.children.remove(child)
            if isinstance(child, AsyncString):
                child._parent = None
        except ValueError:
            pass

    def remove_all(self):
        '''Remove all :attr:`children`.'''
        if self._children:
            for child in self._children:
                if isinstance(child, AsyncString):
                    child._parent = None
            self._children = []

    def append_to(self, parent):
        '''Append itself to ``parent``. Return ``self``.'''
        parent.append(self)
        return self

    def stream(self, request):
        '''An iterable over strings or asynchronous elements.

        This is the most important method of an :class:`AsyncString`.
        It is called by :meth:`http_response` or by the :attr:`parent`
        of this :class:`AsyncString`.
        It returns an iterable (list, tuple or a generator) over
        strings (``unicode/str`` for python 2, ``str`` only for python 3) or
        :ref:`asynchronous elements <tutorials-coroutine>` which result in
        strings. This method can be called **once only**, otherwise a
        :class:`RuntimeError` occurs.

        This method should not be overwritten, instead one should use the
        :meth:`do_stream` to customise behaviour.
        '''
        if self._streamed:
            raise RuntimeError('%s already streamed' % self)
        self._streamed = True
        return self.do_stream(request)

    def do_stream(self, request):
        '''Returns an iterable over strings or asynchronous components.

        If :ref:`asynchronous elements <tutorials-coroutine>` are included
        in the iterable, when called, they must result in strings.
        This method can be re-implemented by subclasses and should not be
        invoked directly.
        Use the :meth:`stream` method instead.
        '''
        if self._children:
            for child in self._children:
                if isinstance(child, AsyncString):
                    for bit in child.stream(request):
                        yield bit
                else:
                    yield child

    def http_response(self, request, *stream):
        '''Return a :class:`.WsgiResponse` or a :class:`~asyncio.Future`.

        This method asynchronously wait for :meth:`stream` and subsequently
        returns a :class:`.WsgiResponse`.
        '''
        response = request.response
        response.content_type = self.content_type
        if stream:
            stream = stream[0]
        else:
            stream = multi_async(self.stream(request))
            if stream.done():
                stream = stream.result()
            else:
                return chain_future(
                    stream, callback=partial(self.http_response, request))
        response.content = self.to_string(stream)
        return response

    def to_string(self, streams):
        '''Called to transform the collection of
        ``streams`` into the content string.
        This method can be overwritten by derived classes.

        :param streams: a collection (list or dictionary) containing
            ``strings/bytes`` used to build the final ``string/bytes``.
        :return: a string or bytes
        '''
        return to_string(''.join(stream_to_string(streams)))

    def render(self, request=None):
        '''Render this string.

        This method returns a string or a :class:`~asyncio.Future` which
        results in a string. On the other hand, the callable method of
        a :class:`.AsyncString` **always** returns a :class:`~asyncio.Future`.
        '''
        stream = multi_async(self.stream(request))
        if stream.done():
            return self.to_string(stream.result())
        else:
            return chain_future(stream, callback=self.to_string)

    def __call__(self, request):
        stream = multi_async(self.stream(request))
        return chain_future(stream, callback=self.to_string)


class Json(AsyncString):
    '''An :class:`AsyncString` which renders into a json string.

    The :attr:`AsyncString.content_type` attribute is set to
    ``application/json``.

    .. attribute:: as_list

        If ``True``, the content is always a list of objects.
        Default ``False``.

    .. attribute:: parameters

        Additional dictionary of parameters passed during initialisation.
    '''
    _default_content_type = 'application/json'

    def _setup(self, as_list=False, **params):
        self.as_list = as_list
        super(Json, self)._setup(**params)

    def do_stream(self, request):
        if self._children:
            for child in self._children:
                if isinstance(child, AsyncString):
                    for bit in child.stream(request):
                        yield bit
                elif isinstance(child, Mapping):
                    yield stream_mapping(child, request)
                else:
                    yield child

    def to_string(self, stream):
        if len(stream) == 1 and not self.as_list:
            return json.dumps(stream[0])
        else:
            return json.dumps(stream)


def html_factory(tag, **defaults):
    '''Returns an :class:`Html` factory function for ``tag`` and a given
    dictionary of ``defaults`` parameters. For example::

    >>> input_factory = html_factory('input', type='text')
    >>> html = input_factory(value='bla')

    '''
    def html_input(*children, **params):
        p = defaults.copy()
        p.update(params)
        return Html(tag, *children, **p)
    return html_input


class Html(AsyncString):
    '''An :class:`AsyncString` for ``html`` content.

    The :attr:`~AsyncString.content_type` attribute is set to ``text/html``.

    :param tag: Set the :attr:`tag` attribute. Must be given and can be
        ``None``.
    :param children: Optional children which will be added via the
        :meth:`~AsyncString.append` method.
    :param params: Optional keyed-value parameters
        including:

        * ``cn`` class name or list of class names.
        * ``attr`` dictionary of attributes to add.
        * ``data`` dictionary of data to add (rendered as HTML data attribute).
        * ``type`` type of element, only supported for tags which accept the
          ``type`` attribute (for example the ``input`` tag).
    '''
    _default_content_type = 'text/html'

    def __init__(self, tag, *children, **params):
        self._tag = tag
        self._extra = {}
        self._setup(**params)
        for child in children:
            self.append(child)

    @property
    def tag(self):
        '''The tag for this HTML element.

        One of ``div``, ``a``, ``table`` and so forth.
        It can be ``None``.
        '''
        return self._tag

    @property
    def _classes(self):
        if 'classes' in self._extra:
            return self._extra['classes']

    @property
    def _data(self):
        if 'data' in self._extra:
            return self._extra['data']

    @property
    def _attr(self):
        if 'attr' in self._extra:
            return self._extra['attr']

    @property
    def _css(self):
        if 'css' in self._extra:
            return self._extra['css']

    @property
    def type(self):
        if 'attr' in self._extra:
            return self._extra['attr'].get('type')

    def get_form_value(self):
        '''Return the value of this :class:`Html` element when it is contained
        in a Html form element.

        For most element it gets the ``value`` attribute.
        '''
        return self._visitor.get_form_value(self)

    def set_form_value(self, value):
        '''Set the value of this :class:`Html` element when it is contained
        in a Html form element.
        For most element it sets the ``value`` attribute.'''
        self._visitor.set_form_value(self, value)

    def __repr__(self):
        if self._tag and self._tag in INLINE_TAGS:
            return '<%s%s/>' % (self._tag, self.flatatt())
        elif self._tag:
            return '<%s%s>' % (self._tag, self.flatatt())
        else:
            return self.__class__.__name__

    def append(self, child):
        if child not in (None, self):
            tag = child_tag(self._tag)
            if tag:
                if isinstance(child, Html):
                    if child.tag != tag:
                        child = Html(tag, child)
                elif not child.startswith('<%s' % tag):
                    child = Html(tag, child)
            super(Html, self).append(child)

    def _setup(self, cn=None, attr=None, css=None, data=None,
               content_type=None, **params):
        self.charset = params.get('charset') or 'utf-8'
        self._content_type = content_type or self._default_content_type
        self._visitor = html_visitor(self._tag)
        self.addClass(cn)
        self.data(data)
        self.attr(attr)
        self.css(css)
        self.attr(params)

    def attr(self, *args):
        '''Add the specific attribute to the attribute dictionary
        with key ``name`` and value ``value`` and return ``self``.'''
        attr = self._attr
        if not args:
            return attr or {}
        result, adding = self._attrdata('attr', *args)
        if adding:
            if attr is None:
                self._extra['attr'] = attr = {}
            attr.update(result)
            result = self
        return result

    def data(self, *args):
        '''Add or retrieve data values for this :class:`Html`.'''
        data = self._data
        if not args:
            return data or {}
        result, adding = self._attrdata('data', *args)
        if adding:
            if data is None:
                self._extra['data'] = {}
            add = self._visitor.add_data
            for key, value in iteritems(result):
                add(self, key, value)
            return self
        else:
            return result

    def addDir(self, key, value=None):
        '''Add a directive to the element

        The ``value`` can be ``None``.
        '''
        attr = self._attr
        if attr is None:
            self._extra['attr'] = attr = {}
        attr[key] = value or ''
        return self

    def addClass(self, cn):
        '''Add the specific class names to the class set and return ``self``.
        '''
        if cn:
            if isinstance(cn, (tuple, list, set, frozenset)):
                add = self.addClass
                for c in cn:
                    add(c)
            else:
                classes = self._classes
                if classes is None:
                    self._extra['classes'] = classes = set()
                add = classes.add
                for cn in cn.split():
                    add(slugify(cn))
        return self

    def hasClass(self, cn):
        '''``True`` if ``cn`` is a class of self.'''
        classes = self._classes
        return classes and cn in classes

    def removeClass(self, cn):
        '''Remove classes'''
        if cn:
            ks = self._classes
            if ks:
                for cn in cn.split():
                    if cn in ks:
                        ks.remove(cn)
        return self

    def flatatt(self, **attr):
        '''Return a string with atributes to add to the tag'''
        cs = ''
        attr = self._attr
        classes = self._classes
        data = self._data
        css = self._css
        attr = attr.copy() if attr else {}
        if classes:
            cs = ' '.join(classes)
            attr['class'] = cs
        if css:
            attr['style'] = ' '.join(('%s:%s;' % (k, v) for
                                      k, v in css.items()))
        if data:
            for k, v in data.items():
                attr['data-%s' % k] = dump_data_value(v)
        if attr:
            return ''.join(attr_iter(attr))
        else:
            return ''

    def css(self, mapping=None):
        '''Update the css dictionary if ``mapping`` is a dictionary, otherwise
 return the css value at ``mapping``. If ``mapping`` is not given, return the
 whole ``css`` dictionary if available.'''
        css = self._css
        if mapping is None:
            return css
        elif isinstance(mapping, Mapping):
            if css is None:
                self._extra['css'] = css = {}
            css.update(mapping)
            return self
        else:
            return css.get(mapping) if css else None

    def hide(self):
        '''Same as jQuery hide method.'''
        self.css({'display': 'none'})
        return self

    def show(self):
        '''Same as jQuery show method.'''
        css = self._css
        if css:
            css.pop('display', None)
        return self

    def add_media(self, request):
        '''Invoked just before streaming this content.

        It can be used to add media entries to the document.

        TODO: more docs
        '''
        pass

    def do_stream(self, request):
        self.add_media(request)
        if self._tag and self._tag in INLINE_TAGS:
            yield '<%s%s>' % (self._tag, self.flatatt())
        else:
            if self._tag:
                yield '<%s%s>' % (self._tag, self.flatatt())
            if self._children:
                for child in self._children:
                    if isinstance(child, AsyncString):
                        for bit in child.stream(request):
                            yield bit
                    elif isgenerator(child):
                        yield async(child, getattr(request, '_loop', None))
                    else:
                        yield child
            if self._tag:
                yield '</%s>' % self._tag

    def _attrdata(self, cont, name, *val):
        if not name:
            return None, False
        if isinstance(name, Mapping):
            if val:
                raise TypeError('Cannot set a value to %s' % name)
            return name, True
        else:
            if val:
                if len(val) == 1:
                    return {name: val[0]}, True
                else:
                    raise TypeError('Too may arguments')
            else:
                cont = self._extra.get(cont)
                return cont.get(name) if cont else None, False


class Media(AsyncString):
    '''A container for both :class:`.Links` and :class:`.Scripts`.

    .. attribute:: media_path

        The base url path to the local media files, for example
        ``/media/``. Must include both slashes.

    .. attribute:: minified

        Optional flag indicating if relative media files should be modified to
        end with ``.min.js`` or ``.min.css`` rather than ``.js`` or ``.css``
        rispectively.

        Default: ``False``

    .. attribute:: known_libraries

        Optional dictionary of known media libraries, mapping a name to a
        valid absolute or local url. For example::

            known_libraries = {'jquery':
                               '//code.jquery.com/jquery-1.9.1.min.js'}

        Default: ``None``
    '''
    mediatype = None

    def __init__(self, media_path, minified=False, known_libraries=None,
                 dependencies=None):
        super(Media, self).__init__()
        self.media_path = media_path
        self.minified = minified
        if known_libraries is None:
            known_libraries = media_libraries()['libs']
        if dependencies is None:
            dependencies = media_libraries()['deps']
        self.known_libraries = known_libraries
        self.dependencies = dependencies

    def is_relative(self, path):
        '''Check if ``path`` is a local relative path.

        A path is local relative when it does not start with a slash
        ``/`` nor ``http://`` nor ``https://``.
        '''
        return not (path.startswith('http://') or path.startswith('https://')
                    or path.startswith('/'))

    def absolute_path(self, path, with_media_ending=True):
        '''Return a suitable absolute url for ``path``.

        The url is calculated in the following way:

        * Check if ``path`` is an entry in the :attr:`known_libraries`
          dictionary. In this case replace ``path`` with
          ``known_libraries[path]``.
        * If ``path`` :meth:`is_relative` build a sutable url by prepending
          the :attr:`media_path` attribute.

        :return: A url path to insert in a HTML ``link`` or ``script``.
        '''
        ending = '.%s' % self.mediatype
        minify = True
        urlparams = ''
        if isinstance(path, dict):
            urlparams = path.get('urlparams', urlparams)
            minify = path.get('minify', minify)
            path = path['url']
        if path in self.known_libraries:
            lib = self.known_libraries[path]
            if isinstance(lib, dict):
                urlparams = lib.get('urlparams', '')
                minify = lib.get('minify', minify)
                lib = lib['url']
            path = '%s%s' % (lib, ending)
        if self.minified and minify:
            if path.endswith(ending):
                path = self._minify(path, ending)
        if not with_media_ending:
            path = path[:-len(ending)]
        if urlparams:
            path = '%s?%s' % (path, urlparams)
        if self.is_relative(path):
            return remove_double_slash('%s/%s' % (self.media_path, path))
        else:
            return path

    def _minify(self, path, postfix):
        new_postfix = 'min%s' % postfix
        if not path.endswith(new_postfix):
            path = '%s.%s' % (path[:-len(postfix)], new_postfix)
        return path


class Links(Media):
    '''A :class:`.Media` container for ``link`` tags.

    The ``<link>`` tag defines the relationship between a
    :class:`.HtmlDocument` and an external resource.
    It is most used to link to style sheets.
    '''
    mediatype = 'css'

    def append(self, href=None, rel=None, type=None, media=None,
               condition=None, **kwargs):
        '''Append a link to this container.

        :param href: a string indicating the location of the linked
            document
        :param rel: Specifies the relationship between the document
            and the linked document. If not given ``stylesheet`` is used.
        :param type: Specifies the content type of the linked document.
            If not given ``text/css`` is used.
        :param media: Specifies on what device the linked document will be
            displayed. If not given or ``all``, the media is for all devices.
        :param kwargs: additional attributes
        '''
        if href:
            type = type or 'text/css'
            rel = rel or 'stylesheet'
            path = self.absolute_path(href)
            value = Html('link', href=path, type=type, rel=rel, **kwargs)
            if media not in (None, 'all'):
                value.attr('media', media)
            if condition:
                value = Html(None, '<!--[if %s]>\n' % condition,
                             value, '\n<![endif]-->')
            value = '%s\n' % value.render()
            if value not in self.children:
                self.children.append(value)


class Scripts(Media):
    '''A :class:`.Media` container for ``script`` tags.

    Supports javascript Asynchronous Module Definition
    '''
    mediatype = 'js'

    def __init__(self, *args, **kwargs):
        self.require_callback = kwargs.pop('require_callback', None)
        self.wait = kwargs.pop('wait', 200)
        self.required = []
        self._requirejs = False
        super(Scripts, self).__init__(*args, **kwargs)

    def script(self, src, type=None, **kwargs):
        type = type or 'application/javascript'
        path = self.absolute_path(src)
        return '%s\n' % Html('script', src=path, type=type,
                             **kwargs).render()

    def require(self, *scripts):
        '''Add a ``script`` to the list of :attr:`required` scripts.

        The ``script`` can be a name in the :attr:`~Media.known_libraries`,
        an absolute uri or a relative url.

        The script will be loaded using the ``require`` javascript package.
        '''
        if scripts:
            self.append('require')
        required = self.required
        for script in scripts:
            if script == 'require':
                continue
            name = script
            if isinstance(script, dict):
                name = script['url']
            if name.strip() not in self.known_libraries:
                script = self.absolute_path(script)
            if script not in required:
                required.append(script)

    def append(self, src=None, type=None, **kwargs):
        '''add a new script to the container.

        :param src: a ``string`` representing an absolute path to the script
            or relative path (does not start with ``http`` or ``/``), in which
            case the :attr:`Media.media_path` attribute is prepended.
        '''
        if src:
            if src == 'require':
                self._requirejs = True
            script = self.script(src, type=type, **kwargs)
            if script not in self.children:
                self.children.append(script)

    def require_script(self):
        '''Can be used for requirejs'''
        libs = dict(((key, self.absolute_path(key, False))
                     for key in self.known_libraries))
        return OrderedDict((('deps', self.required),
                            ('paths', libs),
                            ('shim', self.dependencies),
                            ('waitSeconds', self.wait)))

    def do_stream(self, request):
        if self._requirejs or self.required:
            require = self.require_script()
            callback = self.require_callback or ''
            if callback:
                callback = ('\nrequire.callback = function () {%s();}'
                            % callback)
            yield ('<script type="text/javascript">\n'
                   'var require = %s,\n'
                   '    media_path = "%s";%s\n'
                   '</script>\n') % (pyjson.dumps(require),
                                     self.media_path, callback)
        for child in self.children:
            yield child


class Embedded(Html):

    def __init__(self, tag, **kwargs):
        super(Embedded, self).__init__(None, **kwargs)
        self._child_tag = tag
        self._child_kwargs = kwargs

    def append(self, child, media=None):
        if not isinstance(child, Html):
            kwargs = self._child_kwargs
            if media:
                kwargs['media'] = media
            child = Html(self._child_tag, child, **kwargs)
        super(Embedded, self).append(child)


class Head(Html):
    ''':class:`HtmlDocument` ``head`` tag element.

    Contains :class:`Html` attributes for the various part of an HTML
    Head element. The head element is accessed via the
    :attr:`HtmlDocument.head` attribute.

    .. attribute:: title

        Text in the ``title`` tag.

    .. attribute:: meta

        A container of :class:`Html` ``meta`` tags.
        To add new meta tags use the
        :meth:`add_meta` method rather than accessing the :attr:`meta`
        attribute directly.

    .. attribute:: links

        A :class:`.Links` container.

        Rendered just after the :attr:`meta` container.

    .. attribute:: embedded_css

        Css embedded in the html page.

        Rendered just after the :attr:`links` container

    .. attribute:: embedded_js

        Javascript embedded in the html page.

        Rendered just after the :attr:`embedded_css` container

    .. attribute:: scripts

        A :class:`.Scripts` container.

        Rendered just after the :attr:`embedded_js` container.
        To add new javascript files simply use the :meth:`~.Scripts.append`
        method on this attribute. You can add relative paths::

            html.head.scripts.append('/media/js/scripts.js')

        as well as absolute paths::

            html.head.scripts.append(
                'https://ajax.googleapis.com/ajax/libs/jquery/1.7.2/jquery.js')

    '''
    def __init__(self, media_path=None, title=None, meta=None, minified=False,
                 known_libraries=None, scripts_dependencies=None,
                 require_callback=None, **params):
        super(Head, self).__init__('head', **params)
        self.title = title
        self.append(Html(None, meta))
        self.append(Links(media_path, minified=minified,
                          known_libraries=known_libraries,
                          dependencies=scripts_dependencies))
        self.append(Embedded('style', type='text/css'))
        self.append(Embedded('script', type='text/javascript'))
        self.append(Scripts(media_path, minified=minified,
                            known_libraries=known_libraries,
                            dependencies=scripts_dependencies,
                            require_callback=require_callback))
        self.add_meta(charset=self.charset)

    @property
    def meta(self):
        return self._children[0]

    def __get_media_path(self):
        return self.links.media_path

    def __set_media_path(self, media_path):
        self.links.media_path = media_path
        self.scripts.media_path = media_path
    media_path = property(__get_media_path, __set_media_path)

    def __get_links(self):
        return self._children[1]

    def __set_links(self, links):
        self._children[1] = links
    links = property(__get_links, __set_links)

    def __get_css(self):
        return self._children[2]

    def __set_css(self, css):
        self._children[2] = css
    embedded_css = property(__get_css, __set_css)

    def __get_js(self):
        return self._children[3]

    def __set_js(self, js):
        self._children[3] = js
    embedded_js = property(__get_js, __set_js)

    def __get_scripts(self):
        return self._children[4]

    def __set_scripts(self, scripts):
        self._children[4] = scripts
    scripts = property(__get_scripts, __set_scripts)

    def do_stream(self, request):
        if self.title:
            self._children.insert(0, '<title>%s</title>' % self.title)
        return super(Head, self).do_stream(request)

    def add_meta(self, **kwargs):
        '''Add a new :class:`Html` meta tag to the :attr:`meta` collection.'''
        meta = Html('meta', **kwargs)
        self.meta.append(meta)

    def get_meta(self, name):
        '''Get the ``content`` attribute of meta tag ``name``.

        For example::

            head.get_meta('decription')

        returns the ``content`` attribute of the meta tag with attribute
        ``name`` equal to ``description`` or ``None``.
        '''
        for child in self.meta._children:
            if child.attr('name') == name:
                return child.attr('content')

    def replace_meta(self, name, content):
        '''Replace the ``content`` attribute of meta tag ``name``

        If the meta with ``name`` is not available, it is added, otherwise
        its content is replaced.
        '''
        for child in self.meta._children:
            if child.attr('name') == name:
                child.attr('content', content)
                return
        self.add_meta(name=name, content=content)

    def __add__(self, other):
        if isinstance(other, Media):
            return Media(media=self).add(other)
        else:
            return self


class HtmlDocument(Html):
    '''An :class:`.Html` component rendered as an HTML5_ document.

    An instance of this class can be obtained via the
    :attr:`.WsgiRequest.html_document` attribute.

    .. attribute:: head

        The :class:`.Head` part of this :class:`HtmlDocument`

    .. attribute:: body

        The body part of this :class:`HtmlDocument`, an :class:`.Html` element

    .. _HTML5: http://www.w3schools.com/html/html5_intro.asp
    '''
    _template = ('<!DOCTYPE html>\n'
                 '<html%s>\n'
                 '%s\n%s'
                 '\n</html>')

    def __init__(self, title=None, media_path='/media/', charset=None,
                 minified=False, known_libraries=None, require_callback=None,
                 scripts_dependencies=None, **params):
        super(HtmlDocument, self).__init__(None, **params)
        self.head = Head(title=title, media_path=media_path, minified=minified,
                         known_libraries=known_libraries,
                         require_callback=require_callback,
                         scripts_dependencies=scripts_dependencies,
                         charset=charset)
        self.body = Html('body')
        self.end = Html(None)

    def do_stream(self, request):
        # stream the body
        self.body.append(self.end)
        body = multi_async(self.body.stream(request))
        # the body has asynchronous components
        # delay the header untl later
        if not body.done():
            yield self._html(request, body)
        else:
            head = multi_async(self.head.stream(request))
            #
            # header not ready (this should never occur really)
            if not head.done():
                yield self._html(request, body, head)
            else:
                yield self._template % (self.flatatt(),
                                        self.head.to_string(head.result()),
                                        self.body.to_string(body.result()))

    def _html(self, request, body, head=None):
        if head is None:
            body = yield body
            head = multi_async(self.head.stream(request))
        head = yield head
        result = self._template % (self.flatatt(),
                                   self.head.to_string(head),
                                   self.body.to_string(body))
        coroutine_return(result)
