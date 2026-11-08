import remarkGfm from 'remark-gfm';

/** Answers use GitHub-flavoured markdown: tables, strikethrough, task lists and bare links. Raw HTML stays off. */
export const remarkPlugins = [remarkGfm];
