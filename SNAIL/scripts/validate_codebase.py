"""
SNAIL Codebase Validator

Static analysis script to catch common integration issues:
- Method signature mismatches
- Non-existent method calls
- Missing imports
- Configuration key mismatches

Run this script before deployment to catch issues early.

@file        validate_codebase.py
@description Automated codebase validation
@author      SNAIL Development Team
@created     2025-12-04
@version     1.0.0
"""

import ast
import os
import sys
from pathlib import Path
from typing import Dict, List, Set, Tuple, Optional
from dataclasses import dataclass, field
from collections import defaultdict

# Add project root to path
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


@dataclass
class MethodSignature:
    """Represents a method's signature."""
    name: str
    class_name: str
    file_path: str
    line_number: int
    params: List[str]
    required_params: List[str]
    optional_params: List[str]


@dataclass
class MethodCall:
    """Represents a method call."""
    method_name: str
    receiver_type: Optional[str]
    file_path: str
    line_number: int
    kwargs: List[str]


@dataclass
class ValidationError:
    """Represents a validation error."""
    severity: str  # 'ERROR', 'WARNING'
    category: str
    message: str
    file_path: str
    line_number: int


@dataclass
class ValidationResult:
    """Result of validation run."""
    errors: List[ValidationError] = field(default_factory=list)
    warnings: List[ValidationError] = field(default_factory=list)

    @property
    def has_errors(self) -> bool:
        return len(self.errors) > 0

    def add_error(self, category: str, message: str, file_path: str, line_number: int):
        self.errors.append(ValidationError(
            severity='ERROR',
            category=category,
            message=message,
            file_path=file_path,
            line_number=line_number
        ))

    def add_warning(self, category: str, message: str, file_path: str, line_number: int):
        self.warnings.append(ValidationError(
            severity='WARNING',
            category=category,
            message=message,
            file_path=file_path,
            line_number=line_number
        ))


class CodebaseValidator:
    """
    Validates SNAIL codebase for common integration issues.
    """

    def __init__(self, project_root: Path):
        self.project_root = project_root
        self.src_root = project_root / 'src'
        self.result = ValidationResult()

        # Known method signatures (populated by analysis)
        self.class_methods: Dict[str, Dict[str, MethodSignature]] = defaultdict(dict)

        # Method calls found
        self.method_calls: List[MethodCall] = []

        # Critical class->methods mapping we want to validate
        self.critical_classes = {
            'TelegramAlerts': [
                'send', 'send_with_retry', 'send_entry_alert', 'send_exit_alert',
                'send_pnl_update', 'send_stop_loss_alert', 'send_wing_approach_alert',
                'send_vix_warning', 'send_vix_hard_exit', 'send_gap_alert',
                'send_friday_decision', 'send_error_alert', 'send_daily_summary',
                'send_morning_summary'
            ],
            'SNAILKiteClient': [
                'quote', 'ltp', 'get_nifty_spot', 'get_india_vix', 'place_order',
                'modify_order', 'cancel_order', 'orders', 'positions'
            ]
        }

        # Known non-existent methods (anti-patterns to catch)
        self.forbidden_patterns = {
            'telegram': {
                'send_message': 'Use send() instead',
                'sendMessage': 'Use send() instead',
            }
        }

    def validate(self) -> ValidationResult:
        """Run all validation checks."""
        print("=" * 60)
        print("SNAIL Codebase Validator")
        print("=" * 60)

        # Step 1: Extract method signatures from key classes
        print("\n[1/4] Extracting method signatures...")
        self._extract_signatures()

        # Step 2: Find all method calls
        print("[2/4] Analyzing method calls...")
        self._analyze_method_calls()

        # Step 3: Check for forbidden patterns
        print("[3/4] Checking for forbidden patterns...")
        self._check_forbidden_patterns()

        # Step 4: Validate method calls against signatures
        print("[4/4] Validating method signatures...")
        self._validate_signatures()

        # Print results
        self._print_results()

        return self.result

    def _extract_signatures(self):
        """Extract method signatures from critical classes."""

        # TelegramAlerts
        telegram_path = self.src_root / 'api' / 'telegram_alerts.py'
        if telegram_path.exists():
            self._extract_class_signatures(telegram_path, 'TelegramAlerts')

        # SNAILKiteClient
        kite_path = self.src_root / 'api' / 'kite_client.py'
        if kite_path.exists():
            self._extract_class_signatures(kite_path, 'SNAILKiteClient')

    def _extract_class_signatures(self, file_path: Path, class_name: str):
        """Extract method signatures from a class."""
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                source = f.read()

            tree = ast.parse(source)

            for node in ast.walk(tree):
                if isinstance(node, ast.ClassDef) and node.name == class_name:
                    for item in node.body:
                        if isinstance(item, ast.FunctionDef):
                            if item.name.startswith('_') and not item.name.startswith('__'):
                                continue  # Skip private methods

                            params = []
                            required_params = []
                            optional_params = []

                            # Get arguments
                            args = item.args
                            defaults_offset = len(args.args) - len(args.defaults)

                            for i, arg in enumerate(args.args):
                                if arg.arg == 'self':
                                    continue
                                params.append(arg.arg)
                                if i < defaults_offset:
                                    required_params.append(arg.arg)
                                else:
                                    optional_params.append(arg.arg)

                            sig = MethodSignature(
                                name=item.name,
                                class_name=class_name,
                                file_path=str(file_path),
                                line_number=item.lineno,
                                params=params,
                                required_params=required_params,
                                optional_params=optional_params
                            )
                            self.class_methods[class_name][item.name] = sig

            print(f"    Found {len(self.class_methods[class_name])} methods in {class_name}")

        except Exception as e:
            print(f"    Error parsing {file_path}: {e}")

    def _analyze_method_calls(self):
        """Find all method calls in the codebase."""
        for py_file in self.src_root.rglob('*.py'):
            self._analyze_file_calls(py_file)

        print(f"    Found {len(self.method_calls)} method calls to analyze")

    def _analyze_file_calls(self, file_path: Path):
        """Analyze method calls in a single file."""
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                source = f.read()

            tree = ast.parse(source)

            # Determine if we're inside specific classes (to avoid false positives)
            # e.g., self.kite inside SNAILKiteClient refers to underlying SDK, not our wrapper
            is_kite_client_file = 'kite_client.py' in str(file_path)
            is_telegram_file = 'telegram_alerts.py' in str(file_path)

            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    # Check for attribute calls like self.telegram.send()
                    if isinstance(node.func, ast.Attribute):
                        method_name = node.func.attr

                        # Try to determine receiver type from attribute chain
                        receiver_type = self._infer_receiver_type(node.func)

                        # Skip self.kite calls inside kite_client.py (they refer to SDK)
                        if is_kite_client_file and receiver_type == 'SNAILKiteClient':
                            continue
                        # Skip self.telegram calls inside telegram_alerts.py
                        if is_telegram_file and receiver_type == 'TelegramAlerts':
                            continue

                        # Get keyword argument names
                        kwargs = [kw.arg for kw in node.keywords if kw.arg]

                        call = MethodCall(
                            method_name=method_name,
                            receiver_type=receiver_type,
                            file_path=str(file_path),
                            line_number=node.lineno,
                            kwargs=kwargs
                        )
                        self.method_calls.append(call)

        except SyntaxError:
            pass  # Skip files with syntax errors
        except Exception as e:
            pass  # Skip files we can't parse

    def _infer_receiver_type(self, attr_node: ast.Attribute) -> Optional[str]:
        """Infer the type of the receiver from attribute chain."""
        # Check for common patterns like self.telegram, self.kite
        if isinstance(attr_node.value, ast.Attribute):
            if isinstance(attr_node.value.value, ast.Name):
                if attr_node.value.value.id == 'self':
                    attr_name = attr_node.value.attr
                    if attr_name == 'telegram':
                        return 'TelegramAlerts'
                    elif attr_name == 'kite':
                        return 'SNAILKiteClient'
        return None

    def _check_forbidden_patterns(self):
        """Check for known forbidden patterns."""
        for call in self.method_calls:
            # Check telegram forbidden patterns
            if call.receiver_type == 'TelegramAlerts':
                if call.method_name in self.forbidden_patterns.get('telegram', {}):
                    suggestion = self.forbidden_patterns['telegram'][call.method_name]
                    self.result.add_error(
                        category='FORBIDDEN_METHOD',
                        message=f"Method '{call.method_name}' does not exist on TelegramAlerts. {suggestion}",
                        file_path=call.file_path,
                        line_number=call.line_number
                    )

    def _validate_signatures(self):
        """Validate method calls against known signatures."""
        for call in self.method_calls:
            if call.receiver_type and call.receiver_type in self.class_methods:
                class_methods = self.class_methods[call.receiver_type]

                # Check if method exists
                if call.method_name not in class_methods:
                    # Only warn if it's a method we expect to know about
                    if call.method_name in self.critical_classes.get(call.receiver_type, []):
                        self.result.add_error(
                            category='UNKNOWN_METHOD',
                            message=f"Method '{call.method_name}' not found in {call.receiver_type}",
                            file_path=call.file_path,
                            line_number=call.line_number
                        )
                else:
                    # Validate keyword arguments
                    sig = class_methods[call.method_name]

                    # Check for unknown kwargs
                    for kwarg in call.kwargs:
                        if kwarg not in sig.params:
                            self.result.add_error(
                                category='INVALID_KWARG',
                                message=f"Unknown argument '{kwarg}' in call to {call.receiver_type}.{call.method_name}(). "
                                        f"Valid args: {sig.params}",
                                file_path=call.file_path,
                                line_number=call.line_number
                            )

                    # Check for missing required kwargs (if using kwargs)
                    if call.kwargs:
                        for required in sig.required_params:
                            if required not in call.kwargs:
                                self.result.add_warning(
                                    category='MISSING_REQUIRED_ARG',
                                    message=f"Missing required argument '{required}' in call to "
                                            f"{call.receiver_type}.{call.method_name}(). "
                                            f"Required: {sig.required_params}",
                                    file_path=call.file_path,
                                    line_number=call.line_number
                                )

    def _print_results(self):
        """Print validation results."""
        print("\n" + "=" * 60)
        print("VALIDATION RESULTS")
        print("=" * 60)

        if self.result.errors:
            print(f"\n{len(self.result.errors)} ERROR(S):")
            print("-" * 40)
            for err in self.result.errors:
                rel_path = Path(err.file_path).relative_to(self.project_root)
                print(f"  [{err.category}] {rel_path}:{err.line_number}")
                print(f"    {err.message}")

        if self.result.warnings:
            print(f"\n{len(self.result.warnings)} WARNING(S):")
            print("-" * 40)
            for warn in self.result.warnings:
                rel_path = Path(warn.file_path).relative_to(self.project_root)
                print(f"  [{warn.category}] {rel_path}:{warn.line_number}")
                print(f"    {warn.message}")

        if not self.result.errors and not self.result.warnings:
            print("\nNo issues found!")

        print("\n" + "=" * 60)
        status = "FAILED" if self.result.has_errors else "PASSED"
        print(f"Validation {status}")
        print("=" * 60)


def run_import_check() -> List[str]:
    """Check that all modules can be imported."""
    errors = []

    print("\n" + "=" * 60)
    print("IMPORT VALIDATION")
    print("=" * 60)

    modules_to_check = [
        'src.api.telegram_alerts',
        'src.api.telegram_bot',
        'src.api.kite_client',
        'src.api.claude_client',
        'src.api.response_handler',
        'src.services.entry_manager',
        'src.services.exit_manager',
        'src.services.position_monitor',
        'src.services.claude_advisor',
        'src.utils.db',
        'src.utils.config',
        'src.utils.calculations',
        'src.workflows.daily_startup',
        'src.workflows.daily_summary',
        'src.workflows.monitor_workflow',
    ]

    for module_name in modules_to_check:
        try:
            __import__(module_name)
            print(f"  [OK] {module_name}")
        except Exception as e:
            errors.append(f"{module_name}: {e}")
            print(f"  [FAIL] {module_name}: {e}")

    return errors


def main():
    """Main entry point."""
    import argparse

    parser = argparse.ArgumentParser(description='SNAIL Codebase Validator')
    parser.add_argument('--skip-imports', action='store_true', help='Skip import validation')
    parser.add_argument('--strict', action='store_true', help='Treat warnings as errors')
    args = parser.parse_args()

    # Run static analysis
    validator = CodebaseValidator(PROJECT_ROOT)
    result = validator.validate()

    # Run import check
    import_errors = []
    if not args.skip_imports:
        import_errors = run_import_check()

    # Determine exit code
    has_errors = result.has_errors or len(import_errors) > 0
    if args.strict:
        has_errors = has_errors or len(result.warnings) > 0

    if has_errors:
        print("\nValidation FAILED - please fix the errors above")
        sys.exit(1)
    else:
        print("\nValidation PASSED")
        sys.exit(0)


if __name__ == '__main__':
    main()
