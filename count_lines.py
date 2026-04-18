import textwrap

test_content = textwrap.dedent("""
    class MyClass:
        def my_method(self):
            '''Instance method.'''
            return "hello"
        
        def another_method(self):
            '''Another instance method.'''
            return "world"

    def standalone_function():
        '''A standalone function.'''
        return 42

    # Create instance and call methods
    obj = MyClass()
    obj.my_method()          # Instance method call - should be found
    obj.another_method()     # Another instance method call - should NOT be found for "MyClass.my_method"
    MyClass.my_method(obj)   # Class-style call - should be found

    # Call standalone function
    standalone_function()    # Should NOT be found for "MyClass.my_method"
""")

lines = test_content.strip().split('\n')
for i, line in enumerate(lines, 1):
    print(f"{i:2}: {line}")