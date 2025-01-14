#!/usr/bin/env python
# -*- coding: utf-8 -*-
# **************************************************************************
# Copyright © 2017-2019 jianglin
# File Name: whoosh_backend.py
# Author: jianglin
# Email: mail@honmaple.com
# Created: 2017-04-15 20:03:27 (CST)
# Last Update: Tuesday 2018-12-18 11:18:50 (CST)
#          By:
# Description:
# **************************************************************************
import os
import sys

import sqlalchemy
from flask_sqlalchemy import models_committed
from sqlalchemy import types
from whoosh import index as whoosh_index
from whoosh.analysis import StemmingAnalyzer
from whoosh.fields import BOOLEAN, DATETIME, ID, NUMERIC, TEXT
from whoosh.fields import Schema as _Schema
from whoosh.qparser import AndGroup, MultifieldParser, OrGroup
from .backends import BaseBackend, BaseSchema, logger, relation_column

DEFAULT_WHOOSH_INDEX_NAME = 'msearch'
DEFAULT_ANALYZER = StemmingAnalyzer()
DEFAULT_PRIMARY_KEY = 'id'

if sys.version_info[0] < 3:
    str = unicode


class Schema(BaseSchema):
    def __init__(self, table, analyzer=None):
        self.table = table
        self.analyzer = getattr(self.table, "__msearch_analyzer__", analyzer)
        self.schema = _Schema(**self.fields)

    def fields_map(self, field_type):
        if field_type == "primary":
            return ID(stored=True, unique=True)
        type_map = {
            'date': types.Date,
            'datetime': types.DateTime,
            'boolean': types.Boolean,
            'integer': types.Integer,
            'float': types.Float
        }
        if isinstance(field_type, str):
            field_type = type_map.get(field_type, types.Text)

        if field_type in (types.DateTime, types.Date):
            return DATETIME(stored=True, sortable=True)
        elif field_type == types.Integer:
            return NUMERIC(stored=True, numtype=int)
        elif field_type == types.Float:
            return NUMERIC(stored=True, numtype=float)
        elif field_type == types.Boolean:
            return BOOLEAN(stored=True)
        return TEXT(stored=True, analyzer=self.analyzer, sortable=False)

    def _fields(self):
        return {DEFAULT_PRIMARY_KEY: ID(stored=True, unique=True)}


class Index(object):
    def __init__(self, name, table, analyzer=None):
        self.name = name
        self.table = table
        self._schema = Schema(table, analyzer)
        self._writer = None
        self._client = self.init()

    def init(self):
        ix_path = os.path.join(self.name, self.table.__table__.name)
        if whoosh_index.exists_in(ix_path):
            return whoosh_index.open_dir(ix_path)
        if not os.path.exists(ix_path):
            os.makedirs(ix_path)
        return whoosh_index.create_in(ix_path, self.schema)

    @property
    def index(self):
        return self

    @property
    def fields(self):
        return self.schema.names()

    @property
    def schema(self):
        return self._schema.schema

    def create(self, *args, **kwargs):
        if self._writer is None:
            self._writer = self._client.writer()
        return self._writer.add_document(**kwargs)

    def update(self, *args, **kwargs):
        if self._writer is None:
            self._writer = self._client.writer()
        return self._writer.update_document(**kwargs)

    def delete(self, *args, **kwargs):
        if self._writer is None:
            self._writer = self._client.writer()
        return self._writer.delete_by_term(**kwargs)

    def commit(self):
        if self._writer is None:
            self._writer = self._client.writer()
        r = self._writer.commit()
        self._writer = None
        return r

    def search(self, *args, **kwargs):
        return self._client.searcher().search(*args, **kwargs)


class WhooshSearch(BaseBackend):
    def init_app(self, app):
        self._indexs = {}
        if self.analyzer is None:
            self.analyzer = DEFAULT_ANALYZER
        self.index_name = app.config.get('MSEARCH_INDEX_NAME',
                                         DEFAULT_WHOOSH_INDEX_NAME)
        if app.config.get('MSEARCH_ENABLE', True):
            models_committed.connect(self._index_signal)
        super(WhooshSearch, self).init_app(app)

    def _index(self, model):
        '''
        get index
        '''
        index_name = self.index_name
        if hasattr(model, "__msearch_index__"):
            index_name = model.__msearch_index__

        name = model
        if not isinstance(model, str):
            name = model.__table__.name
        if name not in self._indexs:
            self._indexs[name] = Index(index_name, model, self.analyzer)
        return self._indexs[name]

    def create_one_index(self,
                         instance,
                         update=False,
                         delete=False,
                         commit=True):
        '''
        :param instance: sqlalchemy instance object
        :param update: when update is True,use `update_document`,default `False`
        :param delete: when delete is True,use `delete_by_term` with id(primary key),default `False`
        :param commit: when commit is True,writer would use writer.commit()
        :raise: ValueError:when both update is True and delete is True
        :return: instance
        '''
        if update and delete:
            raise ValueError("update and delete can't work togther")
        table = instance.__class__
        ix = self._index(table)
        searchable = ix.fields
        attrs = {DEFAULT_PRIMARY_KEY: str(instance.id)}

        for field in searchable:
            if '.' in field:
                attrs[field] = str(relation_column(instance, field.split('.')))
            else:
                attrs[field] = str(getattr(instance, field))
        if delete:
            logger.debug('deleting index: {}'.format(instance))
            ix.delete(fieldname=DEFAULT_PRIMARY_KEY, text=str(instance.id))
        elif update:
            logger.debug('updating index: {}'.format(instance))
            ix.update(**attrs)
        else:
            logger.debug('creating index: {}'.format(instance))
            ix.create(**attrs)
        if commit:
            ix.commit()
        return instance

    def _fields(self, attr):
        return attr

    def msearch(self, m, query, fields=None, limit=None, or_=True, termclass=None):
        '''
        set limit make search faster
        '''
        ix = self._index(m)
        if fields is None:
            fields = ix.fields
        group = OrGroup if or_ else AndGroup
        termclass = (
            termclass
            if termclass
            else getattr(m, "__msearch_termclass__", None)
        )
        termclass = dict(termclass=termclass) if termclass else {}
        parser = MultifieldParser(fields, ix.schema, group=group, **termclass)
        return ix.search(parser.parse(query), limit=limit)

    def _query_class(self, q):
        _self = self

        class Query(q):
            def whoosh_search(self, query, fields=None, limit=None, or_=False):
                logger.warning(
                    'whoosh_search has been replaced by msearch.please use msearch'
                )
                return self.msearch(query, fields, limit, or_)

            def msearch(self, query, fields=None, limit=None, or_=False, termclass=None):
                model = self._mapper_zero().class_
                results = _self.msearch(model, query, fields, limit, or_, termclass=termclass)
                if not results:
                    return self.filter(sqlalchemy.text('null'))
                result_set = set()
                for i in results:
                    result_set.add(i[DEFAULT_PRIMARY_KEY])
                return self.filter(
                    getattr(model, DEFAULT_PRIMARY_KEY).in_(result_set))

        return Query
