/* Local experimental boundary: native parser objects end at this JSON output. */
import io.proleap.cobol.Cobol85Parser;
import io.proleap.cobol.asg.metamodel.Program;
import io.proleap.cobol.asg.metamodel.CompilationUnit;
import io.proleap.cobol.asg.runner.impl.CobolParserRunnerImpl;
import io.proleap.cobol.preprocessor.CobolPreprocessor.CobolSourceFormatEnum;
import org.antlr.v4.runtime.ParserRuleContext;
import org.antlr.v4.runtime.misc.Interval;
import org.antlr.v4.runtime.tree.ParseTree;
import java.io.File;
import java.util.ArrayList;
import java.util.List;

public class SyntaxProbe {
    static List<String> rows = new ArrayList<>();
    static String quote(String text) {
        return "\"" + text.replace("\\", "\\\\").replace("\"", "\\\"")
            .replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t") + "\"";
    }
    static String raw(ParserRuleContext context) {
        return context.getStart().getInputStream().getText(new Interval(
            context.getStart().getStartIndex(), context.getStop().getStopIndex()));
    }
    static List<String> identifiers(ParseTree tree) {
        List<String> result = new ArrayList<>();
        if (tree instanceof Cobol85Parser.IdentifierContext) {
            result.add(raw((ParserRuleContext) tree));
            return result;
        }
        for (int index=0; index<tree.getChildCount(); index++)
            result.addAll(identifiers(tree.getChild(index)));
        return result;
    }
    static String array(List<String> values) {
        List<String> quoted = new ArrayList<>();
        for (String value : values) quoted.add(quote(value));
        return "[" + String.join(",", quoted) + "]";
    }
    static void record(String kind, ParserRuleContext context, List<String> reads,
                       List<String> writes, List<String> expressions, boolean rounded) {
        boolean unsupported = false;
        ParseTree parent = context.getParent();
        while (parent != null) {
            if (parent instanceof Cobol85Parser.IfStatementContext ||
                parent instanceof Cobol85Parser.EvaluateStatementContext ||
                parent instanceof Cobol85Parser.PerformStatementContext) unsupported = true;
            parent = parent.getParent();
        }
        rows.add("{\"kind\":" + quote(kind) + ",\"text\":" + quote(raw(context)) +
            ",\"reads\":" + array(reads) + ",\"writes\":" + array(writes) +
            ",\"expressions\":" + array(expressions) + ",\"rounded\":" + rounded +
            ",\"scope_unsupported\":" + unsupported +
            ",\"start_line\":" + context.getStart().getLine() +
            ",\"end_line\":" + context.getStop().getLine() +
            ",\"start_column\":" + (context.getStart().getCharPositionInLine()+1) +
            ",\"end_column\":" + (context.getStop().getCharPositionInLine()+context.getStop().getText().length()+1) + "}");
    }
    static void visit(ParseTree tree) {
        if (tree instanceof Cobol85Parser.ComputeStatementContext) {
            Cobol85Parser.ComputeStatementContext context = (Cobol85Parser.ComputeStatementContext) tree;
            if (context.onSizeErrorPhrase() != null || context.notOnSizeErrorPhrase() != null) return;
            List<String> writes = new ArrayList<>(), expressions = new ArrayList<>();
            boolean rounded = false;
            for (Cobol85Parser.ComputeStoreContext store : context.computeStore()) {
                writes.add(raw(store.identifier())); expressions.add(raw(context.arithmeticExpression()));
                rounded |= store.ROUNDED() != null;
            }
            record("COMPUTE", context, identifiers(context.arithmeticExpression()), writes, expressions, rounded);
            return;
        }
        if (tree instanceof Cobol85Parser.MoveStatementContext) {
            Cobol85Parser.MoveStatementContext context = (Cobol85Parser.MoveStatementContext) tree;
            if (context.moveToStatement() == null || context.ALL() != null) return;
            Cobol85Parser.MoveToStatementContext body = context.moveToStatement();
            List<String> writes = new ArrayList<>(), expressions = new ArrayList<>();
            for (Cobol85Parser.IdentifierContext target : body.identifier()) {
                writes.add(raw(target)); expressions.add(raw(body.moveToSendingArea()));
            }
            record("MOVE", context, identifiers(body.moveToSendingArea()), writes, expressions, false);
            return;
        }
        if (tree instanceof Cobol85Parser.AddStatementContext) {
            Cobol85Parser.AddStatementContext context = (Cobol85Parser.AddStatementContext) tree;
            if (context.addToGivingStatement() == null || context.onSizeErrorPhrase() != null || context.notOnSizeErrorPhrase() != null) return;
            Cobol85Parser.AddToGivingStatementContext body = context.addToGivingStatement();
            List<String> reads = new ArrayList<>(), writes = new ArrayList<>(), expressions = new ArrayList<>(), inputs = new ArrayList<>();
            for (Cobol85Parser.AddFromContext input : body.addFrom()) {
                reads.addAll(identifiers(input)); inputs.add(raw(input));
            }
            for (Cobol85Parser.AddToGivingContext input : body.addToGiving()) {
                reads.addAll(identifiers(input)); inputs.add(raw(input));
            }
            boolean rounded = false;
            for (Cobol85Parser.AddGivingContext target : body.addGiving()) {
                writes.add(raw(target.identifier())); expressions.add(String.join(" + ", inputs));
                rounded |= target.ROUNDED() != null;
            }
            record("ADD", context, reads, writes, expressions, rounded); return;
        }
        if (tree instanceof Cobol85Parser.GobackStatementContext || tree instanceof Cobol85Parser.ContinueStatementContext) {
            record(tree instanceof Cobol85Parser.GobackStatementContext ? "GOBACK" : "CONTINUE",
                (ParserRuleContext) tree, new ArrayList<>(), new ArrayList<>(), new ArrayList<>(), false); return;
        }
        for (int index=0; index<tree.getChildCount(); index++) visit(tree.getChild(index));
    }
    public static void main(String[] args) throws Exception {
        Program program = new CobolParserRunnerImpl().analyzeFile(new File(args[0]), CobolSourceFormatEnum.TANDEM);
        for (CompilationUnit unit : program.getCompilationUnits()) visit(unit.getCtx());
        System.out.println("{\"native_parser\":\"proleap\",\"version\":\"2.4.0\",\"statements\":[" + String.join(",", rows) + "]}");
    }
}
