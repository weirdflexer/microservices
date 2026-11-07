import {object, uuid} from '../../common-node/validation.mjs';

export function makeRoutes(service) {
  return [
    {method: 'PUT', path: /^\/api\/tables\/([^/]+)\/schedule$/, handle: ({params, body}) => service.setSchedule(uuid(params[0]), body)},
    {method: 'GET', path: /^\/api\/tables\/([^/]+)\/availability$/, handle: ({params, query}) => service.availability(uuid(params[0]), query.get('start_at'), query.get('end_at'))},
    {method: 'POST', path: /^\/api\/holds$/, status: 201, handle: ({body}) => service.hold(body)},
    {method: 'POST', path: /^\/api\/holds\/([^/]+):confirm$/, handle: ({params, body}) => {
      object(body, ['booking_id']); return service.confirm(uuid(params[0]), uuid(body.booking_id));
    }},
    {method: 'DELETE', path: /^\/api\/holds\/([^/]+)$/, handle: ({params, query}) => service.release(uuid(params[0]), uuid(query.get('booking_id')))},
  ];
}
