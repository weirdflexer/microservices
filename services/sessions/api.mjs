import {uuid, pageNumber} from '../../common-node/validation.mjs';

export function makeRoutes(service) {
  return [
    {method: 'GET', path: /^\/api\/sessions$/, handle: ({query}) => service.list(
      query.has('booking_id') ? uuid(query.get('booking_id')) : null,
      pageNumber(query.get('offset'), 0, 0), pageNumber(query.get('limit'), 100, 1, 100))},
    {method: 'GET', path: /^\/api\/sessions\/([^/]+)$/, handle: ({params}) => service.get(uuid(params[0]))},
    {method: 'POST', path: /^\/api\/sessions\/([^/]+):start$/, handle: ({params}) => service.start(uuid(params[0]))},
    {method: 'POST', path: /^\/api\/sessions\/([^/]+):complete$/, handle: ({params}) => service.complete(uuid(params[0]))},
  ];
}
