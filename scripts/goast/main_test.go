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
	source := "//go:build linux\n\npackage demo\n\ntype Box[T any] struct {\n  Value [4]T\n  Callback func(int) error\n}\n\nfunc Apply[T ~int](x T) T { return x + 1 }\n// trailing comment\n\n"
	if err := os.WriteFile(filepath.Join(root, "main.go"), []byte(source), 0644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(root, "invalid.go"), []byte("package demo\nfunc broken(\n"), 0644); err != nil {
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
	if len(report.Files) != 1 || len(report.Diagnostics) != 1 || report.Diagnostics[0].Path != "invalid.go" {
		t.Fatalf("hidden root skipped or parse failure omitted: %+v", report)
	}
	file := report.Files[0]
	if file.LineEnd != 12 || file.ContentHash != fmt.Sprintf("%x", sha256.Sum256([]byte(source))) {
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

func TestSelectionFingerprintsCategoriesAndRepeatedInit(t *testing.T) {
	root := t.TempDir()
	selected := []string{".github/tool.go", "main.go", "tests/check.go"}
	sources := map[string]string{
		".github/tool.go": "// @generated tool\npackage tool\nfunc Tool() {}\n",
		"main.go":         "package demo\nfunc init() { println(1) }\nfunc init() { println(2) }\ntype T struct{}\nfunc (t T) Run() int { return 1 }\n",
		"tests/check.go":  "package tests\nfunc Check() {}\n",
		"ignored.go":      "package ignored\nfunc Ignored() {}\n",
	}
	for name, source := range sources {
		path := filepath.Join(root, name)
		if err := os.MkdirAll(filepath.Dir(path), 0755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(path, []byte(source), 0644); err != nil {
			t.Fatal(err)
		}
	}
	selection := filepath.Join(root, "selection.json")
	scope := map[string]any{"selection_hash": fmt.Sprintf("%x", sha256.Sum256([]byte(strings.Join(selected, "\n"))))}
	data, _ := json.Marshal(map[string]any{"root": root, "files": selected, "scope": scope})
	if err := os.WriteFile(selection, data, 0644); err != nil {
		t.Fatal(err)
	}
	analyze := func() Report {
		t.Helper()
		cmd := exec.Command("go", "run", ".", "-root", root, "-selection", selection)
		output, err := cmd.Output()
		if err != nil {
			t.Fatalf("%v: %s", err, output)
		}
		var report Report
		if err := json.Unmarshal(output, &report); err != nil {
			t.Fatal(err)
		}
		return report
	}
	before := analyze()
	if len(before.Files) != 3 || before.Stats.ByCategory["test"].FileCount != 1 || before.Stats.ByCategory["generated"].FileCount != 1 {
		t.Fatalf("scope/categories: %+v", before)
	}
	a := before.Files[1].Symbols
	if a[0].ID == a[1].ID || a[0].ContentHash == a[1].ContentHash {
		t.Fatalf("repeated init lost: %+v", a)
	}
	changed := "// leading comment\npackage demo\n\nfunc init() {\n println(1)\n}\nfunc init() { println(2) }\ntype T struct{}\nfunc (t T) Run() int { return 1 }\n"
	if err := os.WriteFile(filepath.Join(root, "main.go"), []byte(changed), 0644); err != nil {
		t.Fatal(err)
	}
	after := analyze()
	b := after.Files[1].Symbols
	for i := range a {
		if a[i].ContentHash != b[i].ContentHash || a[i].BodyHash != b[i].BodyHash {
			t.Fatalf("position/comment changed fingerprint: %+v => %+v", a[i], b[i])
		}
	}
	changed = strings.Replace(changed, "(t T)", "(t *T)", 1)
	changed = strings.Replace(changed, "return 1", "return 9", 1)
	if err := os.WriteFile(filepath.Join(root, "main.go"), []byte(changed), 0644); err != nil {
		t.Fatal(err)
	}
	c := analyze().Files[1].Symbols[3]
	if c.SignatureHash == b[3].SignatureHash || c.BodyHash == b[3].BodyHash {
		t.Fatalf("receiver/body change missed: %+v => %+v", b[3], c)
	}
}

func TestTestClassification(t *testing.T) {
	for _, path := range []string{"test_example.go", "case_test.go", "src/spec.test.go", "Tests/example.go", "testdata/demo.go"} {
		if !isTestPath(path) {
			t.Fatalf("missed test path: %s", path)
		}
	}
	if isTestPath("internal/testinghelpers/main.go") {
		t.Fatal("production helper classified as test")
	}
}
