#!/usr/bin/env node
/* Syntax-only JS/TS indexing. Loads the compiler, never the target program. */
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const {createRequire} = require('node:module');

function loadCompiler(input) {
  if (input.typescript) return require(path.resolve(input.typescript));
  for (const root of [input.root, __dirname]) {
    try { return require(createRequire(path.join(root, 'package.json')).resolve('typescript')); }
    catch (error) { if (error.code !== 'MODULE_NOT_FOUND') throw error; }
  }
  throw new Error('TypeScript compiler unavailable; pass --typescript <module directory or typescript.js>');
}

function analyze(ts, root, relative) {
  const raw = fs.readFileSync(path.join(root, relative));
  const text = new TextDecoder('utf-8', {fatal: true}).decode(raw);
  const source = ts.createSourceFile(relative, text, ts.ScriptTarget.Latest, true);
  if (source.parseDiagnostics.length) {
    return {diagnostics: source.parseDiagnostics.map(d => ({path: relative,
      line: source.getLineAndCharacterOfPosition(d.start || 0).line + 1,
      reason: ts.flattenDiagnosticMessageText(d.messageText, '\n')}))};
  }
  const printer = ts.createPrinter({removeComments: true, newLine: ts.NewLineKind.LineFeed});
  const hash = value => crypto.createHash('sha256').update(value).digest('hex');
  const printed = node => printer.printNode(ts.EmitHint.Unspecified, node, source);
  const canonical = node => {
    const children = node.getChildren(source).filter(child => !(child.kind >= ts.SyntaxKind.FirstJSDocNode && child.kind <= ts.SyntaxKind.LastJSDocNode));
    return children.length ? children.map(canonical).join('|') : `${node.kind}:${node.getText(source)}`;
  };
  const headerCanonical = node => {
    const children = node.getChildren(source).filter(child => child !== node.body && !(child.kind >= ts.SyntaxKind.FirstJSDocNode && child.kind <= ts.SyntaxKind.LastJSDocNode));
    return children.length ? children.map(headerCanonical).join('|') : `${node.kind}:${node.getText(source)}`;
  };
  const line = position => source.getLineAndCharacterOfPosition(position).line + 1;
  const imports = new Set(), importDetails = [], exports = [], exportedLocals = new Set();
  const symbols = [];
  const has = (node, kind) => !!node.modifiers?.some(modifier => modifier.kind === kind);
  const addImport = (node, target, kind) => {
    if (!ts.isStringLiteralLike(target)) return;
    imports.add(target.text);
    importDetails.push({path: target.text, line: line(node.getStart(source)), kind});
  };
  const exportNames = (statement, names) => {
    if (!has(statement, ts.SyntaxKind.ExportKeyword)) return;
    const isDefault = has(statement, ts.SyntaxKind.DefaultKeyword);
    for (const name of names) {
      exportedLocals.add(name);
      exports.push({name: isDefault ? 'default' : name, local: name, line: line(statement.getStart(source)), kind: 'declaration'});
    }
  };
  for (const statement of source.statements) {
    if (ts.isImportDeclaration(statement)) addImport(statement, statement.moduleSpecifier, 'import');
    if (ts.isImportEqualsDeclaration(statement) && ts.isExternalModuleReference(statement.moduleReference)
        && statement.moduleReference.expression) addImport(statement, statement.moduleReference.expression, 'require');
    if (ts.isExportDeclaration(statement)) {
      const target = statement.moduleSpecifier;
      if (target) addImport(statement, target, 'reexport');
      if (statement.exportClause && ts.isNamedExports(statement.exportClause)) {
        for (const element of statement.exportClause.elements) {
          const local = (element.propertyName || element.name).text;
          exports.push({name: element.name.text, local, source: target?.text,
                        line: line(element.getStart(source)), kind: target ? 'reexport' : 'named'});
          if (!target) exportedLocals.add(local);
        }
      } else {
        exports.push({name: statement.exportClause?.name?.text || '*', source: target?.text,
                      line: line(statement.getStart(source)), kind: 'reexport'});
      }
    }
    if (ts.isExportAssignment(statement)) {
      const local = ts.isIdentifier(statement.expression) ? statement.expression.text : '<default>';
      exports.push({name: statement.isExportEquals ? 'export=' : 'default', local,
                    line: line(statement.getStart(source)), kind: 'assignment'});
      exportedLocals.add(local);
    }
    if (ts.isVariableStatement(statement)) {
      exportNames(statement, statement.declarationList.declarations.filter(d => ts.isIdentifier(d.name)).map(d => d.name.text));
    } else if (statement.name && ts.isIdentifier(statement.name)) {
      exportNames(statement, [statement.name.text]);
    } else if (has(statement, ts.SyntaxKind.DefaultKeyword)) exportNames(statement, ['<default>']);
  }
  // Literal dynamic imports are dependencies, not proof that a branch executes.
  const walkImports = node => {
    if (ts.isCallExpression(node) && (node.expression.kind === ts.SyntaxKind.ImportKeyword
        || (ts.isIdentifier(node.expression) && node.expression.text === 'require')) && node.arguments[0]) {
      addImport(node, node.arguments[0], 'dynamic-literal');
    }
    ts.forEachChild(node, walkImports);
  };
  walkImports(source);

  function add(node, name, kind, topName, body, callable = node) {
    const start = node.getStart(source), end = node.getEnd();
    const visible = !has(node, ts.SyntaxKind.PrivateKeyword)
      && !(node.name && ts.isPrivateIdentifier(node.name));
    const symbol = {name, kind, exported: exportedLocals.has(topName) && visible, visible,
      line: line(start), end_line: line(Math.max(start, end - 1)), line_quality: 'ast',
      signature: text.slice(start, body ? body.getStart(source) : end).trim(),
      content_hash: hash(canonical(node)), doc: (node.jsDoc || []).map(doc => doc.comment || '').filter(x => typeof x === 'string').join('\n')};
    if (callable.parameters) symbol.params = callable.parameters.map(p => printed(p)).join(', ');
    if (callable.type) symbol.results = printed(callable.type);
    symbol.signature_hash = hash(headerCanonical(node));
    if (body) symbol.body_hash = hash(canonical(body));
    symbols.push(symbol);
  }
  function objectMembers(object, parent, topName) {
    for (const member of object.properties) {
      if (!member.name) continue;
      const name = parent + '.' + member.name.getText(source).replace(/^['"]|['"]$/g, '');
      if (ts.isMethodDeclaration(member) || ts.isGetAccessorDeclaration(member) || ts.isSetAccessorDeclaration(member)) {
        add(member, name, 'method', topName, member.body);
      } else if (ts.isPropertyAssignment(member) && (ts.isArrowFunction(member.initializer) || ts.isFunctionExpression(member.initializer))) {
        add(member, name, 'method', topName, member.initializer.body, member.initializer);
      }
    }
  }
  function statements(nodes, parent = '') {
    for (const node of nodes) {
      const local = node.name?.getText(source) || '<default>';
      const name = parent + local;
      if (ts.isFunctionDeclaration(node)) add(node, name, 'func', local, node.body);
      else if (ts.isClassDeclaration(node) || ts.isInterfaceDeclaration(node)) {
        add(node, name, ts.isClassDeclaration(node) ? 'class' : 'interface', local);
        for (const member of node.members) {
          const memberName = member.name?.getText(source) || (ts.isConstructorDeclaration(member) ? 'constructor' : null);
          if (!memberName) continue;
          if (ts.isMethodDeclaration(member) || ts.isMethodSignature(member) || ts.isConstructorDeclaration(member)
              || ts.isGetAccessorDeclaration(member) || ts.isSetAccessorDeclaration(member)) {
            add(member, name + '.' + memberName, 'method', local, member.body);
          } else if (ts.isPropertyDeclaration(member) && member.initializer
                     && (ts.isArrowFunction(member.initializer) || ts.isFunctionExpression(member.initializer))) {
            add(member, name + '.' + memberName, 'method', local, member.initializer.body, member.initializer);
          }
        }
      } else if (ts.isTypeAliasDeclaration(node) || ts.isEnumDeclaration(node)) {
        add(node, name, ts.isEnumDeclaration(node) ? 'enum' : 'type', local);
      } else if (ts.isVariableStatement(node)) {
        for (const declaration of node.declarationList.declarations) {
          if (!ts.isIdentifier(declaration.name)) continue;
          const localName = declaration.name.text, value = declaration.initializer;
          const isFunction = value && (ts.isArrowFunction(value) || ts.isFunctionExpression(value));
          add(declaration, parent + localName, isFunction ? 'func' :
              (node.declarationList.flags & ts.NodeFlags.Const ? 'const' : 'var'), localName, isFunction ? value.body : null, isFunction ? value : declaration);
          if (value && ts.isObjectLiteralExpression(value)) objectMembers(value, parent + localName, localName);
        }
      } else if (ts.isExportAssignment(node) && (ts.isArrowFunction(node.expression) || ts.isFunctionExpression(node.expression))) {
        add(node.expression, '<default>', 'func', '<default>', node.expression.body);
      }
    }
  }
  statements(source.statements);
  // CommonJS exports are explicit metadata; dynamic object construction is not resolved.
  for (const node of source.statements) {
    if (!ts.isExpressionStatement(node) || !ts.isBinaryExpression(node.expression)
        || node.expression.operatorToken.kind !== ts.SyntaxKind.EqualsToken) continue;
    const left = node.expression.left.getText(source), right = node.expression.right;
    if (left === 'module.exports' || /^(?:module\.)?exports\./.test(left)) {
      if (ts.isIdentifier(right)) exportedLocals.add(right.text);
      if (ts.isArrowFunction(right) || ts.isFunctionExpression(right)) {
        exportedLocals.add(left);
        add(right, left, 'func', left, right.body);
      }
      if (ts.isObjectLiteralExpression(right)) {
        for (const property of right.properties) {
          const local = ts.isShorthandPropertyAssignment(property) ? property.name.text :
            (property.initializer && ts.isIdentifier(property.initializer) ? property.initializer.text : null);
          if (local) exportedLocals.add(local);
          exports.push({name: property.name?.getText(source), local, line: line(property.getStart(source)), kind: 'commonjs'});
        }
      } else exports.push({name: left, local: ts.isIdentifier(right) ? right.text : undefined,
                           line: line(node.getStart(source)), kind: 'commonjs'});
    }
  }
  for (const symbol of symbols) {
    const local = symbol.name.split('.')[0];
    if ((exportedLocals.has(local) || exportedLocals.has(symbol.name)) && symbol.visible) symbol.exported = true;
    delete symbol.visible;
  }
  return {file: {path: relative, language: /\.(?:ts|tsx|mts|cts)$/.test(relative) ? 'typescript' : 'javascript',
    package: path.posix.dirname(relative) === '.' ? '' : path.posix.dirname(relative),
    imports: [...imports].sort(), import_details: importDetails, exports,
    top_level_hash: hash(source.statements.filter(n => !(ts.isFunctionDeclaration(n) || ts.isClassDeclaration(n)
      || ts.isInterfaceDeclaration(n) || ts.isTypeAliasDeclaration(n) || ts.isEnumDeclaration(n)
      || ts.isVariableStatement(n))).map(canonical).join('|')),
    symbols: symbols.sort((a, b) => a.line - b.line || a.name.localeCompare(b.name)),
    line_end: (text.match(/\n/g) || []).length + (text.length && !text.endsWith('\n') ? 1 : 0),
    content_hash: hash(raw), line_quality: 'ast', parser: {name: 'typescript', version: ts.version}}, diagnostics: []};
}

try {
  const input = JSON.parse(fs.readFileSync(0, 'utf8'));
  let ts;
  try { ts = loadCompiler(input); }
  catch (error) { process.stdout.write(JSON.stringify({unavailable: error.message})); process.exit(2); }
  const files = [], diagnostics = [];
  for (const relative of input.files) {
    try {
      const result = analyze(ts, input.root, relative);
      if (result.file) files.push(result.file);
      diagnostics.push(...result.diagnostics);
    } catch (error) { diagnostics.push({path: relative, reason: error.message}); }
  }
  process.stdout.write(JSON.stringify({files, diagnostics, parser_version: ts.version}));
} catch (error) {
  process.stderr.write(error.stack || String(error)); process.exit(1);
}
