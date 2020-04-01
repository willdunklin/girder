import cherrypy
import json
from bson import json_util
from girder import events
from girder.api import access, rest
from girder.api.v1.folder import Folder as FolderResource
from girder.constants import AccessType, TokenScope
from girder.exceptions import ValidationException
from girder.models.folder import Folder
from girder.models.item import Item
from girder.plugin import GirderPlugin


def _validateFolder(event):
    doc = event.info

    if 'isVirtual' in doc and not isinstance(doc['isVirtual'], bool):
        raise ValidationException('The isVirtual field must be boolean.', field='isVirtual')

    if doc.get('isVirtual'):
        # Make sure it doesn't have children
        if list(Folder().childItems(doc, limit=1)):
            raise ValidationException(
                'Virtual folders may not contain child items.', field='isVirtual')
        if list(Folder().find({
            'parentId': doc['_id'],
            'parentCollection': 'folder'
        }, limit=1)):
            raise ValidationException(
                'Virtual folders may not contain child folders.', field='isVirtual')
    if doc['parentCollection'] == 'folder':
        parent = Folder().load(event.info['parentId'], force=True, exc=True)
        if parent.get('isVirtual'):
            raise ValidationException(
                'You may not place folders under a virtual folder.', field='folderId')

    if 'virtualItemsQuery' in doc:
        try:
            json.loads(doc['virtualItemsQuery'])
        except (TypeError, ValueError):
            raise ValidationException(
                'The virtual items query must be valid JSON.', field='virtualItemsQuery')

    if 'virtualItemsSort' in doc:
        try:
            json.loads(doc['virtualItemsSort'])
        except (TypeError, ValueError):
            raise ValidationException(
                'The virtual items sort must be valid JSON.', field='virtualItemsSort')


def _validateItem(event):
    parent = Folder().load(event.info['folderId'], force=True, exc=True)
    if parent.get('isVirtual'):
        raise ValidationException(
            'You may not place items under a virtual folder.', field='folderId')


@rest.boundHandler
def _folderUpdate(self, event):
    params = event.info['params']
    if {'isVirtual', 'virtualItemsQuery', 'virtualItemsSort'} & set(params):
        folder = Folder().load(event.info['returnVal']['_id'], force=True)
        update = False

        if params.get('isVirtual') is not None:
            update = True
            folder['isVirtual'] = params['isVirtual']
        if params.get('virtualItemsQuery') is not None:
            update = True
            folder['virtualItemsQuery'] = params['virtualItemsQuery']
        if params.get('virtualItemsSort') is not None:
            update = True
            folder['virtualItemsSort'] = params['virtualItemsSort']

        if update:
            self.requireAdmin(self.getCurrentUser(), 'Must be admin to setup virtual folders.')
            folder = Folder().filter(Folder().save(folder), rest.getCurrentUser())
            event.preventDefault().addResponse(folder)


@access.public(scope=TokenScope.DATA_READ)
@rest.boundHandler
def _virtualChildItems(self, event):
    params = event.info['params']

    if not params.get('folderId'):
        return  # This is not a child listing request

    user = self.getCurrentUser()
    folder = Folder().load(params['folderId'], user=user, level=AccessType.READ)

    if not folder or not folder.get('isVirtual') or 'virtualItemsQuery' not in folder:
        return  # Parent is not a virtual folder, proceed as normal

    limit, offset, sort = self.getPagingParameters(params, defaultSortField='name')
    q = json_util.loads(folder['virtualItemsQuery'])
    # Add other parameter options with $and to ensure they don't clobber the
    # virtual items query.
    if params.get('text'):
        q = {'$and': [q, {'$text': {'$search': params['text']}}]}
    if params.get('name'):
        q = {'$and': [q, {'name': params['name']}]}

    if 'virtualItemsSort' in folder:
        sort = json.loads(folder['virtualItemsSort'])

    item = Item()
    # These items may reside in folders that the user cannot read, so we must
    # find with permissions
    items = item.findWithPermissions(
        q, sort=sort, user=user, level=AccessType.READ, limit=limit, offset=offset)
    # We have to add this here, as we can't use filtermodel since we return the
    # results in addResponse.
    if callable(getattr(items, 'count', None)):
        cherrypy.response.headers['Girder-Total-Count'] = items.count()
    items = [item.filter(i, user) for i in items]
    event.preventDefault().addResponse(items)


class VirtualFoldersPlugin(GirderPlugin):
    DISPLAY_NAME = 'Virtual Folders'

    def load(self, info):
        name = 'virtual_folders'
        events.bind('model.folder.validate', name, _validateFolder)
        events.bind('model.item.validate', name, _validateItem)
        events.bind('rest.get.item.before', name, _virtualChildItems)
        events.bind('rest.post.folder.after', name, _folderUpdate)
        events.bind('rest.put.folder/:id.after', name, _folderUpdate)

        Folder().exposeFields(level=AccessType.READ, fields={'isVirtual'})
        Folder().exposeFields(level=AccessType.SITE_ADMIN, fields={
            'virtualItemsQuery', 'virtualItemsSort'})

        for endpoint in (FolderResource.updateFolder, FolderResource.createFolder):
            (endpoint.description
                .param('isVirtual', 'Whether this is a virtual folder.', required=False,
                       dataType='boolean')
                .param('virtualItemsQuery', 'Query to use to do virtual item lookup, as JSON.',
                       required=False)
                .param('virtualItemsSort', 'Sort to use during virtual item lookup, as JSON.',
                       required=False))
