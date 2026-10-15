import { tr } from './i18n';

/** Display only controlled codes, never a driver error or credential-bearing form. */
export function remoteGraphError(cause: unknown): string {
  const code = typeof cause === 'string' ? cause : cause && typeof cause === 'object' && 'code' in cause ? String(cause.code) : '';
  const messages: Record<string, string> = {
    REMOTE_GRAPH_NOT_SELECTED: tr('Select a remote connection.', '请选择远程连接'),
    REMOTE_GRAPH_NOT_FOUND: tr('Connection removed. Select again.', '连接已删除，请重新选择'),
    REMOTE_GRAPH_CHANGED: tr('Connection changed. Refresh and select again.', '连接已修改，请刷新后重新选择'),
    REMOTE_GRAPH_INVALID_CONFIG: tr('Check the database URI, database name, username and password.', '请检查数据库 URI、数据库名称、用户名和密码'),
    REMOTE_GRAPH_UNREACHABLE: tr('Could not reach the database.', '无法连接数据库'),
    REMOTE_GRAPH_AUTH_FAILED: tr('Authentication failed. Check the credentials.', '认证失败，请检查凭据'),
    REMOTE_GRAPH_ACCESS_DENIED: tr('The database denied access to this operation.', '数据库拒绝访问此操作'),
    REMOTE_GRAPH_QUERY_REJECTED: tr('This query is not permitted.', '该查询不符合执行约束'),
    REMOTE_GRAPH_QUERY_ERROR: tr('Query failed. Check its syntax.', '查询失败，请检查语法'),
    REMOTE_GRAPH_EXPORT_FAILED: tr('File publication failed. The export is not delivered; please retry.', '文件发布失败，导出尚未交付，请重试'),
    REMOTE_GRAPH_EXPORT_INCOMPLETE: tr('The complete result exceeds the export budget. Narrow the scope or use ordered pagination; no partial file was published.', '完整结果超过导出预算，请缩小范围或使用有序分页；未发布残缺文件'),
    REMOTE_GRAPH_EXPORT_ORDER_REQUIRED: tr('Pagination requires unique projected ORDER BY columns and no SKIP, LIMIT or UNION.', '分页导出需按返回列中的唯一键 ORDER BY；重复键或预设 SKIP、LIMIT、UNION 不可用'),
    REMOTE_GRAPH_TIMEOUT: tr('Query timed out. Reduce its scope.', '查询超时，请缩小范围'),
    REMOTE_GRAPH_SECRET_UNAVAILABLE: tr('The server credential key is unavailable.', '服务端凭据密钥不可用'),
  };
  return messages[code] ? `${messages[code]} (${code})` : tr('Operation failed. Please try again.', '操作失败，请重试');
}
