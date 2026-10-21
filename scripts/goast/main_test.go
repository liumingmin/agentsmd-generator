package main

import (
	"crypto/sha256"
	"encoding/json"
	"fmt"
	"go/ast"
	"go/parser"
	"go/token"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"unicode/utf8"
)

func TestTypeRepresentationPreservesContract(t *testing.T) {
	for _, source := range []string{"[4]int", "<-chan string", "chan<- int", "Box[string]", "map[string]func(int) (bool, error)"} {
		expr, err := parser.ParseExpr(source)
		if err != nil {
			t.Fatal(err)
		}
		got := exprString(expr)
		if got != source {
			t.Fatalf("type contract lost: %q => %q", source, got)
		}
	}
}

func TestRouteRetainsRegistrationChain(t *testing.T) {
	expr, err := parser.ParseExpr(`group.POST("/items", auth.Check, api.Save)`)
	if err != nil {
		t.Fatal(err)
	}
	route, ok := extractRoute(token.NewFileSet(), expr.(*ast.CallExpr), "routes.go")
	if !ok || route.Receiver != "group" || route.Handler != "api.Save" || len(route.Handlers) != 2 || route.Handlers[0] != "auth.Check" || route.Confidence != "candidate" {
		t.Fatalf("lost middleware/receiver or certainty: %+v", route)
	}
}

func TestCronDoesNotTruncateSecondsOrInventDescriptors(t *testing.T) {
	for _, source := range []string{"0 */5 * * * *", "@every 2h30m", "@daily", "// 0 1 * * *"} {
		got, ok := parseCron(source)
		expected := strings.TrimPrefix(source, "// ")
		if !ok || got != expected {
			t.Fatalf("%q => %q (%v)", source, got, ok)
		}
	}
	for _, source := range []string{"@param value", "@every", "@every nonsense", "// 1 2 3 4 5 text"} {
		if _, ok := parseCron(source); ok {
			t.Fatalf("false cron candidate: %q", source)
		}
	}
}

func TestUnicodeDocTruncation(t *testing.T) {
	group := &ast.CommentGroup{List: []*ast.Comment{{Text: "// " + strings.Repeat("文", 121)}}}
	if got := firstLine(group); !utf8.ValidString(got) {
		t.Fatal("truncation broke UTF-8")
	}
}

func TestAnalyzerCLIIncludesTrailingLinesAndDiagnostics(t *testing.T) {
	root := filepath.Join(t.TempDir(), ".project")
	if err := os.MkdirAll(root, 0755); err != nil {
		t.Fatal(err)
	}
	source := "//go:build linux\n\npackage demo\n\ntype Box[T any] struct {\n  Value [4]T\n  Callback func(int) error\n}\n\nfunc Apply[T ~int](x T) T { return x + 1 }\nfunc register() { router.GET(\"/prod\", handler) }\nvar schedule = \"0 0 * * *\"\n// trailing comment\n\n"
	if err := os.WriteFile(filepath.Join(root, "main.go"), []byte(source), 0644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(root, "invalid.go"), []byte("package demo\nfunc broken(\n"), 0644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(root, "routes_test.go"), []byte("package demo\nfunc TestRoute() { router.GET(\"/test\", handler); _ = \"@daily\" }\n"), 0644); err != nil {
		t.Fatal(err)
	}
	output := filepath.Join(root, "report.json")
	cmd := exec.Command("go", "run", ".", "-root", root, "-out", output)
	if data, err := cmd.CombinedOutput(); err != nil {
		t.Fatalf("analyzer: %v\n%s", err, data)
	}
	data, err := os.ReadFile(output)
	if err != nil {
		t.Fatal(err)
	}
	var report Report
	if err := json.Unmarshal(data, &report); err != nil {
		t.Fatal(err)
	}
	if len(report.Files) != 2 || len(report.Diagnostics) != 1 || report.Diagnostics[0].Path != "invalid.go" {
		t.Fatalf("hidden root skipped or parse failure omitted: %+v", report)
	}
	if len(report.Routes) != 2 || report.Routes[0].IsTest || !report.Routes[1].IsTest || len(report.Crons) != 2 || report.Crons[0].IsTest || !report.Crons[1].IsTest {
		t.Fatalf("lost test origin: routes=%+v crons=%+v", report.Routes, report.Crons)
	}
	file := report.Files[0]
	if file.LineEnd != 14 || file.ContentHash != fmt.Sprintf("%x", sha256.Sum256([]byte(source))) {
		t.Fatalf("wrong physical range/hash: %+v", file)
	}
	if len(file.BuildConstraints) != 1 {
		t.Fatalf("lost build constraints: %+v", file)
	}
	symbols := map[string]Symbol{}
	for _, symbol := range file.Symbols {
		symbols[symbol.Name] = symbol
	}
	if symbols["Box"].Fields[0].Type != "[4]T" || symbols["Box"].Fields[1].Type != "func(int) error" {
		t.Fatalf("lost field contract: %+v", symbols["Box"])
	}
	if !strings.Contains(symbols["Apply"].Signature, "~int") {
		t.Fatalf("lost generic constraint: %+v", symbols["Apply"])
	}
	if !strings.Contains(symbols["Box"].Signature, "[T any]") {
		t.Fatalf("lost type parameter: %+v", symbols["Box"])
	}
}
