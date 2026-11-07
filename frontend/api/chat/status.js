import {forward} from '../../server/forward.mjs';
export async function GET(request){return forward(request,'/api/chat/status');}
