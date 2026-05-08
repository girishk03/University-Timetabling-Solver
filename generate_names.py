import json
import random

# Lists of names
first_names = [
    "James", "Mary", "John", "Patricia", "Robert", "Jennifer", "Michael", "Linda",
    "William", "Elizabeth", "David", "Barbara", "Richard", "Susan", "Joseph",
    "Jessica", "Thomas", "Sarah", "Charles", "Karen", "Christopher", "Nancy",
    "Daniel", "Lisa", "Matthew", "Betty", "Anthony", "Margaret", "Mark",
    "Sandra", "Donald", "Ashley", "Steven", "Kimberly", "Paul", "Emily",
    "Andrew", "Donna", "Joshua", "Michelle", "Kenneth", "Dorothy", "Kevin",
    "Carol", "Brian", "Amanda", "George", "Melissa", "Timothy", "Deborah",
    "Ronald", "Stephanie", "Edward", "Rebecca", "Jason", "Sharon", "Jeffrey",
    "Laura", "Ryan", "Cynthia", "Jacob", "Kathleen", "Gary", "Amy", "Nicholas",
    "Angela", "Eric", "Shirley", "Jonathan", "Anna", "Stephen", "Brenda",
    "Larry", "Pamela", "Justin", "Emma", "Scott", "Nicole", "Brandon", "Helen"
]

last_names = [
    "Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller",
    "Davis", "Rodriguez", "Martinez", "Hernandez", "Lopez", "Gonzalez",
    "Wilson", "Anderson", "Thomas", "Taylor", "Moore", "Jackson", "Martin",
    "Lee", "Perez", "Thompson", "White", "Harris", "Sanchez", "Clark",
    "Ramirez", "Lewis", "Robinson", "Walker", "Young", "Allen", "King",
    "Wright", "Scott", "Torres", "Nguyen", "Hill", "Flores", "Green",
    "Adams", "Nelson", "Baker", "Hall", "Rivera", "Campbell", "Mitchell"
]

course_prefixes = [
    "Introduction to", "Advanced", "Principles of", "Fundamentals of",
    "Applied", "Modern", "Theoretical", "Practical", "Data", "Software",
    "Computer", "Information", "Business", "Management", "Engineering"
]

course_subjects = [
    "Computer Science", "Data Structures", "Algorithms", "Databases",
    "Machine Learning", "Artificial Intelligence", "Web Development",
    "Cybersecurity", "Networks", "Operating Systems", "Software Engineering",
    "Cloud Computing", "Data Analytics", "Programming", "System Design",
    "Mathematics", "Statistics", "Linear Algebra", "Calculus", "Discrete Math",
    "Physics", "Chemistry", "Biology", "Economics", "Finance", "Marketing",
    "Management", "Accounting", "Psychology", "Sociology", "Philosophy",
    "Literature", "History", "Geography", "Political Science"
]

course_suffixes = ["I", "II", "III", "A", "B", "Lab", "Seminar", "Workshop", ""]

def generate_student_name():
    return f"{random.choice(first_names)} {random.choice(last_names)}"

def generate_course_name():
    prefix = random.choice(course_prefixes)
    subject = random.choice(course_subjects)
    suffix = random.choice(course_suffixes)
    if suffix:
        return f"{prefix} {subject} {suffix}".strip()
    return f"{prefix} {subject}".strip()

def main():
    # Load the solution file
    with open("full_solution.json", "r") as f:
        data = json.load(f)
    
    # Get unique student IDs
    students = data.get("students", {})
    student_ids = sorted(students.keys(), key=lambda x: int(x))
    
    # Get unique course IDs from class_to_course mapping
    class_to_course = data.get("class_to_course", {})
    course_ids = sorted(set(class_to_course.values()), key=lambda x: int(x))
    
    # Generate student names
    student_names = {}
    for sid in student_ids:
        student_names[sid] = generate_student_name()
    
    # Generate course names
    course_names = {}
    for cid in course_ids:
        course_names[cid] = generate_course_name()
    
    # Save files
    with open("student_names.json", "w") as f:
        json.dump(student_names, f, indent=2)
    
    with open("course_names.json", "w") as f:
        json.dump(course_names, f, indent=2)
    
    print(f"Generated {len(student_names)} student names -> student_names.json")
    print(f"Generated {len(course_names)} course names -> course_names.json")
    
    # Print samples
    print("\nSample student names:")
    for sid in list(student_names.keys())[:5]:
        print(f"  {sid}: {student_names[sid]}")
    
    print("\nSample course names:")
    for cid in list(course_names.keys())[:5]:
        print(f"  {cid}: {course_names[cid]}")

if __name__ == "__main__":
    main()
