import {forward} from '../server/forward.mjs';
export const maxDuration=130;
export async function POST(request){return forward(request,'/api/chat');}
